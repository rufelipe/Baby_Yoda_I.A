from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv

load_dotenv()

PASTA_BASE = "base"

def criar_db():
    # Carregar documentos PDF da pasta base
    carregador = PyPDFDirectoryLoader(PASTA_BASE, glob="*.pdf")
    documentos = carregador.load()

    # Dividir em chunks
    separador = RecursiveCharacterTextSplitter(
        chunk_size=10000,
        chunk_overlap=2000,
        length_function=len,
        add_start_index=True
    )
    chunks = separador.split_documents(documentos)
    print(f"{len(chunks)} chunks criados")

    # Criar banco vetorial persistente
    db = Chroma.from_documents(chunks, OpenAIEmbeddings(), persist_directory="db")
    print("Banco vetorial criado e salvo em 'db'")

if __name__ == "__main__":
    criar_db()
