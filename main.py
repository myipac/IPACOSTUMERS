import os
import re

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()


# CONFIGURAÇÕES DE BUSCA
NAME_EMP:str = r"(?<=\d\d\d).*"
CLEAR_NAME:str = r"(?<=-).*"

# CONFIGURA SCOPO DO PROJETO -> ESTAMOS DANDO ACESSO TOTAL A APLICAÇÃO PARA FAZER 
SCOPES = ["https://www.googleapis.com/auth/drive"]


def basedir() -> str:
    return os.path.abspath(os.path.normcase(os.path.join(__file__, '..')))


# --- LÓGICA DE VARREDURADA DO SERVIDOR

def scanZ()->list[str]:
    
    basePathZ: str = os.getenv("PATH_ACTIVE_CLIENTS")
    
    
    listEmp:list = []
    
    for item in os.listdir(basePathZ):
        
        caminho = os.path.join(basePathZ, item)
        
        if os.path.isfile(caminho):
            
            # print("continuando")
            continue
        
        
        else:
            
            listEmp.append(item)
        
        new_listEmp:list = []
        
        for emps in listEmp[:-2]:
            
            # tirando o código de empresa e  
            names = re.search(NAME_EMP,emps)

            namesClead = re.search(CLEAR_NAME,names.group())
            
            #print(namesClead.group().strip())
            
            new_listEmp.append(namesClead.group().strip())

    return new_listEmp 


def mapper_group_company(listEmps: list[str] = scanZ()):

    # criando uma hashmap
    hashmap: dict = dict()

    for names in listEmps:

        # pega a primeira palavra do nome da empresa
        first_name = names.split()[0]

        #print(first_name)

        if first_name in hashmap:
            
            if first_name in names:
                
                hashmap[first_name].append(names)

        else:

            hashmap[first_name] = [names]

    return hashmap
               

# --- LÓGICA DE UTILIZAÇÃO DA API


def getServiceAccountCredentials(basedir: str, name_file_key: str = 'credenciais.json'):
    """
    Autentica via Service Account, sem necessidade de login manual no navegador.
    Não usa refresh_token nem expira como o fluxo de usuário.
    """
    key_path = os.path.join(basedir,'credentials',name_file_key)

    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"Arquivo '{name_file_key}' não encontrado em {basedir}. "
            "Gere a chave JSON no Google Cloud Console (IAM > Contas de serviço)."
        )

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES
    )

    return creds


def listPaths():

    creds = getServiceAccountCredentials(basedir=basedir())

    service = build("drive", "v3", credentials=creds)

    folders = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for folder in response.get("files", []):
            
            print(f'Pasta encontrada: {folder.get("name")}, {folder.get("id")}')
        
        folders.extend(response.get("files", []))
        
        page_token = response.get("nextPageToken", None)
        
        if page_token is None:
            
            break

    return folders


if __name__ == "__main__":
    
    '''
    try:
        
        listPaths()
        
        #for path in listPaths():
            #print(path)
    
    except HttpError as error:
        print(f"Ocorreu um erro na API do Google Drive: {error}")
        
    '''
    
    #scanZ()
    
    # for key,value in  mapper_group_company().items():
        
        # print(f"{key} ---> {value}")