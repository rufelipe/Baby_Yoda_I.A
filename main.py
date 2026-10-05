from flask import Flask, request, jsonify
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import time
import xml.etree.ElementTree as ET
from io import BytesIO

app = Flask(__name__)


def carregar_config_local():
    """Lê as configurações Gemini de .env sem exigir python-dotenv."""
    try:
        with open(".env", "r") as arquivo:
            for linha in arquivo:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                nome, valor = linha.split("=", 1)
                nome = nome.strip()
                valor = valor.strip().strip("'\"")
                if nome in ("GEMINI_API_KEY", "GEMINI_MODELS") and not os.environ.get(nome):
                    os.environ[nome] = valor
    except IOError:
        pass


def extrair_texto(arquivo):
    nome = arquivo.filename or "documento"
    conteudo = arquivo.read()
    extensao = os.path.splitext(nome)[1].lower()

    if extensao == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(conteudo)) as documento:
                xml = documento.read("word/document.xml")
            raiz = ET.fromstring(xml)
            return " ".join(
                no.text for no in raiz.iter()
                if no.tag.endswith("}t") and no.text
            )
        except (KeyError, zipfile.BadZipFile, ET.ParseError):
            return ""

    if extensao == ".txt":
        return conteudo.decode("utf-8", errors="ignore")

    if extensao == ".pdf":
        # Extrai strings de texto simples de PDFs; PDFs complexos ou digitalizados
        # precisam de uma ferramenta de extração/OCR no servidor.
        texto = conteudo.decode("latin-1", errors="ignore")
        trechos = re.findall(r"\(((?:\\.|[^\\)])*)\)", texto)
        return " ".join(
            re.sub(r"\\([\\()])", r"\1", trecho)
            for trecho in trechos
        )

    return ""


def consultar_ia_online(pergunta, contexto):
    carregar_config_local()
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        return "Chave GEMINI_API_KEY não configurada no ambiente ou no arquivo .env."
    if not contexto.strip():
        return (
            "Não consegui extrair texto das fontes. O formato .doc antigo não é suportado; "
            "converta o arquivo para .docx ou PDF e tente novamente."
        )

    modelos_padrao = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite"
    ]
    modelos_configurados = os.environ.get("GEMINI_MODELS", "")
    modelos = []
    for item in modelos_configurados.split(",") + modelos_padrao:
        modelo = item.strip()
        if modelo.startswith("models/"):
            modelo = modelo[len("models/"):]
        if modelo and modelo not in modelos:
            modelos.append(modelo)

    if not modelos:
        return "Nenhum modelo foi configurado em GEMINI_MODELS no arquivo .env."

    corpo = {
        "systemInstruction": {
            "parts": [{
                "text": "Responda em português, de forma direta e curta, usando somente o texto das fontes."
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": "Pergunta: {}\n\nFontes:\n{}".format(pergunta, contexto[:24000])}]
        }],
        "generationConfig": {"temperature": 0.2}
    }
    corpo_json = json.dumps(corpo).encode("utf-8")
    erros_modelos = []
    resultado = None

    for modelo in modelos:
        url_base = (
            "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
        ).format(urllib.parse.quote(modelo, safe="-._"))
        url = url_base + "?" + urllib.parse.urlencode({"key": chave})

        # Uma segunda tentativa para falhas temporárias; depois, tenta o próximo modelo.
        for tentativa in range(2):
            req = urllib.request.Request(
                url,
                data=corpo_json,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resposta:
                    resultado = json.loads(resposta.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as erro:
                detalhe = erro.read().decode("utf-8", errors="replace")
                if erro.code == 404:
                    erros_modelos.append("{}: HTTP 404 (modelo indisponível)".format(modelo))
                    break
                if erro.code in (429, 500, 502, 503, 504):
                    if tentativa == 0:
                        time.sleep(1)
                        continue
                    erros_modelos.append("{}: HTTP {} (indisponível/sobrecarregado)".format(modelo, erro.code))
                    break
                return "Erro da API Gemini (HTTP {}): {}".format(erro.code, detalhe[:500])
            except (urllib.error.URLError, TimeoutError) as erro:
                if tentativa == 0:
                    time.sleep(1)
                    continue
                erros_modelos.append("{}: timeout/erro de conexão ({})".format(modelo, erro))
                break
            except ValueError as erro:
                return "Resposta inválida da API Gemini: {}".format(erro)

        if resultado is not None:
            break

    if resultado is None:
        return (
            "Não foi possível obter resposta: todos os modelos disponíveis foram "
            "tentados. Confira GEMINI_MODELS e a disponibilidade dos modelos para sua chave. "
            "Falhas: {}"
        ).format("; ".join(erros_modelos[:4]))

    candidatos = resultado.get("candidates", [])
    partes = candidatos[0].get("content", {}).get("parts", []) if candidatos else []
    texto = "".join(parte.get("text", "") for parte in partes).strip()
    return texto or "A API Gemini não retornou uma resposta de texto."


@app.after_request
def permitir_cors(resposta):
    resposta.headers["Access-Control-Allow-Origin"] = "*"
    resposta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resposta.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resposta


@app.route("/perguntar", methods=["POST", "OPTIONS"])
def perguntar():
    if request.method == "OPTIONS":
        return "", 204

    if request.is_json:
        dados = request.get_json(silent=True) or {}
        pergunta = (dados.get("pergunta") or dados.get("message") or "").strip()
        contexto = dados.get("contexto", "")
    else:
        pergunta = (request.form.get("pergunta") or request.form.get("message") or "").strip()
        partes = []
        for arquivo in request.files.getlist("files"):
            texto = extrair_texto(arquivo)
            if texto.strip():
                partes.append("[{}]\n{}".format(arquivo.filename, texto))
        contexto = "\n\n".join(partes)

    if not pergunta:
        return jsonify({"response": "Envie uma pergunta.", "resposta": "Envie uma pergunta."}), 400

    resposta = consultar_ia_online(pergunta, contexto)
    return jsonify({"response": resposta, "resposta": resposta})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
