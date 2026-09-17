# -*- coding: utf-8 -*-
"""
Cria no Google Drive a estrutura de pastas dos grupos empresariais.

Modelo da arvore (Opcao B - pasta comum compartilhada com a service account):

    <ROOT_FOLDER_ID>/
        Grupo <Nome do Grupo>/        <- pasta-mae (uma por chave do COMPANY_GROUPS)
            <Unidade 1>/              <- subpasta (uma por item da lista)
            <Unidade 2>/
            ...

Requisitos tecnicos atendidos:
  1. Get-or-Create : antes de criar, faz files().list filtrando por name + parent
                     + mimeType (folder) + trashed=false. Se existir, reusa o id.
  2. Resiliencia   : decorator de Exponential Backoff (tenacity) nas chamadas
                     .execute(), tratando HTTP 403/429 e 5xx (rate limit / picos).
  3. Estrangulamento: time.sleep(0.2) apos cada requisicao de CRIACAO, para nao
                      disparar o filtro de spam/burst do Google.

Uso:
    python create_structure.py --dry-run     # so simula, nao cria nada
    python create_structure.py                # executa de verdade
    python create_structure.py --only "Coco Bambu" "Marietta"   # subconjunto

O script e idempotente e retomavel: rodar de novo nao duplica pastas ja criadas.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

# Import do hash map (mesmo diretorio deste arquivo).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from company_groups import COMPANY_GROUPS  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIGURACOES
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

GROUP_PREFIX = "GRUPO "        # prefixo aplicado a pasta-mae de cada grupo (tudo em maiusculas)
SLEEP_AFTER_CREATE = 0.2       # estrangulamento base (segundos) apos cada criacao
CREDENTIALS_FILE = "credenciais.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("drive-structure")


# ---------------------------------------------------------------------------
# CAMINHOS / CREDENCIAIS
# ---------------------------------------------------------------------------
def project_root() -> str:
    """Raiz do projeto = pasta pai desta (Create/ -> raiz)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_service_account_credentials(name_file_key: str = CREDENTIALS_FILE):
    """
    Autentica via Service Account (sem login manual no navegador,
    sem refresh_token e sem expiracao do fluxo de usuario).
    """
    key_path = os.path.join(project_root(), "credentials", name_file_key)
    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"Arquivo '{name_file_key}' nao encontrado em {key_path}. "
            "Gere a chave JSON no Google Cloud Console (IAM > Contas de servico)."
        )
    return service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)


def build_service():
    creds = get_service_account_credentials()
    # cache_discovery=False evita warning e I/O desnecessario.
    return build("drive", "v3", credentials=creds, cache_discovery=False), creds


# ---------------------------------------------------------------------------
# RESILIENCIA (Exponential Backoff) - Requisito 2
# ---------------------------------------------------------------------------
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    """Reintenta apenas erros transitorios de rate limit / servidor."""
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in _RETRYABLE_STATUS
    # Erros de transporte (timeout/conexao) tambem sao transitorios.
    return isinstance(exc, (ConnectionError, TimeoutError))


api_backoff = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=60),  # 2s, 4s, 8s ... ate 60s
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


@api_backoff
def execute(request):
    """Executa uma request da API com backoff exponencial em erros transitorios."""
    return request.execute()


# ---------------------------------------------------------------------------
# GET-OR-CREATE - Requisito 1
# ---------------------------------------------------------------------------
def _escape_query_value(value: str) -> str:
    r"""Escapa \ e ' para uso seguro dentro da query 'q' da API do Drive."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_folder(service, name: str, parent_id: str) -> str | None:
    """Procura uma pasta por nome exato dentro de parent_id. Retorna id ou None."""
    safe = _escape_query_value(name)
    query = (
        f"name = '{safe}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false"
    )
    response = execute(
        service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=10,
            supportsAllDrives=True,           # funciona em pasta comum e Drive Compartilhado
            includeItemsFromAllDrives=True,
        )
    )
    files = response.get("files", [])
    return files[0]["id"] if files else None


def create_folder(service, name: str, parent_id: str) -> str:
    """Cria uma pasta dentro de parent_id e devolve o id. Aplica o sleep de burst."""
    body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    folder = execute(
        service.files().create(body=body, fields="id, name", supportsAllDrives=True)
    )
    time.sleep(SLEEP_AFTER_CREATE)  # Requisito 3 - estrangulamento base
    return folder["id"]


def get_or_create_folder(service, name: str, parent_id: str, cache: dict, dry_run: bool):
    """
    Padrao Get-or-Create com cache O(1) por (parent_id, name):
      - se ja resolvido nesta execucao -> retorna do cache;
      - senao consulta a API; se existir reusa; se nao, cria.
    Retorna (folder_id, status) onde status in {'cache', 'existe', 'criada', 'dry-run'}.
    """
    cache_key = (parent_id, name)
    if cache_key in cache:
        return cache[cache_key], "cache"

    # Em dry-run, se o pai e sintetico (ainda nao existe), o filho tambem nao pode
    # existir -> evita consultar a API com um parent_id invalido (que causaria 404).
    if dry_run and str(parent_id).startswith("DRYRUN::"):
        fake_id = f"DRYRUN::{parent_id}/{name}"
        cache[cache_key] = fake_id
        return fake_id, "dry-run"

    existing = find_folder(service, name, parent_id)
    if existing:
        cache[cache_key] = existing
        return existing, "existe"

    if dry_run:
        # Nao cria; usa um id sintetico so para encadear os filhos na simulacao.
        fake_id = f"DRYRUN::{parent_id}/{name}"
        cache[cache_key] = fake_id
        return fake_id, "dry-run"

    new_id = create_folder(service, name, parent_id)
    cache[cache_key] = new_id
    return new_id, "criada"


# ---------------------------------------------------------------------------
# PRE-VOO: valida acesso a pasta raiz (falha rapido com mensagem clara)
# ---------------------------------------------------------------------------
def preflight(service, root_id: str, creds) -> None:
    client_email = getattr(creds, "service_account_email", "(desconhecido)")
    logger.info("Service account: %s", client_email)
    try:
        meta = execute(
            service.files().get(
                fileId=root_id,
                fields="id, name, mimeType",
                supportsAllDrives=True,
            )
        )
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        if status in (403, 404):
            raise SystemExit(
                f"[ERRO] Pasta raiz '{root_id}' inacessivel (HTTP {status}).\n"
                f"       Compartilhe essa pasta com o e-mail da service account\n"
                f"       ({client_email}) como EDITOR e tente novamente."
            )
        raise
    if meta.get("mimeType") != FOLDER_MIME:
        raise SystemExit(f"[ERRO] O ID raiz '{root_id}' nao e uma pasta.")
    logger.info("Pasta raiz OK: '%s' (%s)", meta.get("name"), meta.get("id"))


# ---------------------------------------------------------------------------
# ORQUESTRACAO
# ---------------------------------------------------------------------------
def build_structure(service, groups: dict, root_id: str, dry_run: bool, only: list | None):
    cache: dict = {}
    stats = {"grupos": 0, "unidades": 0, "criadas": 0, "existentes": 0}

    if only:
        faltando = [g for g in only if g not in groups]
        if faltando:
            logger.warning("Grupos ignorados (nao existem no mapa): %s", faltando)
        groups = {k: v for k, v in groups.items() if k in only}

    total = len(groups)
    for idx, (group_name, units) in enumerate(groups.items(), start=1):
        parent_name = f"{GROUP_PREFIX}{group_name.upper()}"  # pasta-mae em maiusculas
        group_id, gstatus = get_or_create_folder(service, parent_name, root_id, cache, dry_run)
        stats["grupos"] += 1
        if gstatus == "criada":
            stats["criadas"] += 1
        elif gstatus == "existe":
            stats["existentes"] += 1
        logger.info("[%d/%d] %-8s | %s", idx, total, gstatus.upper(), parent_name)

        for unit in units:
            _, ustatus = get_or_create_folder(service, unit, group_id, cache, dry_run)
            stats["unidades"] += 1
            if ustatus == "criada":
                stats["criadas"] += 1
            elif ustatus == "existe":
                stats["existentes"] += 1
            logger.info("        - %-8s | %s", ustatus.upper(), unit)

    return stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Cria a estrutura de pastas dos grupos no Google Drive.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula: consulta a API mas NAO cria nenhuma pasta.")
    parser.add_argument("--only", nargs="+", metavar="GRUPO",
                        help="Processa apenas os grupos informados (nome exato da chave).")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_dotenv(os.path.join(project_root(), ".env"))

    root_id = os.getenv("ROOT_FOLDER_ID")
    if not root_id:
        raise SystemExit("[ERRO] Defina ROOT_FOLDER_ID no arquivo .env.")

    if args.dry_run:
        logger.info(">>> MODO DRY-RUN: nenhuma pasta sera criada. <<<")

    service, creds = build_service()
    preflight(service, root_id, creds)

    try:
        stats = build_structure(service, COMPANY_GROUPS, root_id, args.dry_run, args.only)
    except HttpError as error:
        logger.error("Falha na API do Google Drive: %s", error)
        return 1

    logger.info("=" * 60)
    logger.info("CONCLUIDO | grupos: %(grupos)d | unidades: %(unidades)d | "
                "criadas: %(criadas)d | ja existiam: %(existentes)d", stats)
    if args.dry_run:
        logger.info("(dry-run: os numeros de 'criadas' aparecem como 0 pois nada foi gravado)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
