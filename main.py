from flask import Flask, request, jsonify
import json
import os
import re
import base64
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import time
import xml.etree.ElementTree as ET
import zlib
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


def _ler_valor_pdf(dados, posicao):
    """Lê um valor simples de um stream PDF e retorna (valor, nova posição)."""
    tamanho = len(dados)
    while posicao < tamanho:
        caractere = dados[posicao]
        if caractere in b" \t\r\n\f\x00":
            posicao += 1
        elif caractere == ord("%"):
            while posicao < tamanho and dados[posicao] not in b"\r\n":
                posicao += 1
        else:
            break

    if posicao >= tamanho:
        return None, posicao

    inicio = posicao
    caractere = dados[posicao]

    if caractere == ord("("):
        posicao += 1
        profundidade = 1
        resultado = bytearray()
        while posicao < tamanho and profundidade:
            caractere = dados[posicao]
            posicao += 1
            if caractere == ord("\\") and posicao < tamanho:
                escapado = dados[posicao]
                posicao += 1
                traducoes = {
                    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t",
                    ord("b"): b"\b", ord("f"): b"\f"
                }
                if escapado in traducoes:
                    resultado.extend(traducoes[escapado])
                elif escapado in (ord("\\"), ord("("), ord(")")):
                    resultado.append(escapado)
                elif escapado in (ord("\r"), ord("\n")):
                    if escapado == ord("\r") and posicao < tamanho and dados[posicao] == ord("\n"):
                        posicao += 1
                elif ord("0") <= escapado <= ord("7"):
                    octal = bytearray([escapado])
                    for _ in range(2):
                        if posicao < tamanho and ord("0") <= dados[posicao] <= ord("7"):
                            octal.append(dados[posicao])
                            posicao += 1
                        else:
                            break
                    resultado.append(int(octal, 8) & 255)
                else:
                    resultado.append(escapado)
            elif caractere == ord("("):
                profundidade += 1
                resultado.append(caractere)
            elif caractere == ord(")"):
                profundidade -= 1
                if profundidade:
                    resultado.append(caractere)
            else:
                resultado.append(caractere)
        return ("string", bytes(resultado)), posicao

    if caractere == ord("["):
        posicao += 1
        itens = []
        while posicao < tamanho:
            while posicao < tamanho and dados[posicao] in b" \t\r\n\f\x00":
                posicao += 1
            if posicao < tamanho and dados[posicao] == ord("]"):
                return ("array", itens), posicao + 1
            item, nova_posicao = _ler_valor_pdf(dados, posicao)
            if nova_posicao <= posicao:
                break
            if item is not None:
                itens.append(item)
            posicao = nova_posicao
        return ("array", itens), posicao

    if caractere == ord("<") and posicao + 1 < tamanho and dados[posicao + 1] != ord("<"):
        fim = dados.find(b">", posicao + 1)
        if fim < 0:
            return ("string", b""), tamanho
        hexadecimal = re.sub(rb"\s+", b"", dados[posicao + 1:fim])
        if len(hexadecimal) % 2:
            hexadecimal += b"0"
        try:
            valor = bytes.fromhex(hexadecimal.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            valor = b""
        return ("string", valor), fim + 1

    while posicao < tamanho and dados[posicao] not in b" \t\r\n\f\x00()<>[]{}/%":
        posicao += 1
    if posicao == inicio:
        posicao += 1
    return ("word", dados[inicio:posicao]), posicao


def _decodificar_texto_pdf(valor):
    if valor[:2] in (bytes((254, 255)), bytes((255, 254))):
        return valor.decode("utf-16", errors="replace")
    try:
        return valor.decode("utf-8")
    except UnicodeDecodeError:
        return valor.decode("cp1252", errors="replace")


def _texto_de_stream_pdf(dados):
    tokens = []
    posicao = 0
    while posicao < len(dados):
        token, nova_posicao = _ler_valor_pdf(dados, posicao)
        if nova_posicao <= posicao:
            break
        if token is not None:
            tokens.append(token)
        posicao = nova_posicao

    trechos = []
    for indice, token in enumerate(tokens):
        if token[0] != "word" or token[1] not in (b"Tj", b"TJ", b"'", b'"') or indice == 0:
            continue
        operando = tokens[indice - 1]
        if operando[0] == "string":
            trechos.append(_decodificar_texto_pdf(operando[1]))
        elif operando[0] == "array":
            trechos.extend(
                _decodificar_texto_pdf(item[1])
                for item in operando[1] if item[0] == "string"
            )
    return " ".join(trecho for trecho in trechos if trecho.strip())


def _extrair_texto_pdf(conteudo):
    trechos = []
    for correspondencia in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", conteudo, re.S):
        stream = correspondencia.group(1)
        inicio = max(0, correspondencia.start() - 600)
        cabecalho = conteudo[inicio:correspondencia.start()]
        filtros = re.findall(rb"/([A-Za-z0-9]+Decode)", cabecalho)

        for filtro in filtros:
            try:
                if filtro == b"ASCII85Decode":
                    dados_ascii85 = stream.strip()
                    if dados_ascii85.startswith(b"<~"):
                        dados_ascii85 = dados_ascii85[2:]
                    if dados_ascii85.endswith(b"~>"):
                        dados_ascii85 = dados_ascii85[:-2]
                    stream = base64.a85decode(dados_ascii85, adobe=False)
                elif filtro == b"FlateDecode":
                    try:
                        stream = zlib.decompress(stream)
                    except zlib.error:
                        stream = zlib.decompress(stream, -zlib.MAX_WBITS)
                else:
                    stream = b""
                    break
            except (ValueError, zlib.error):
                stream = b""
                break

        if not filtros:
            try:
                stream = zlib.decompress(stream)
            except zlib.error:
                try:
                    stream = zlib.decompress(stream, -zlib.MAX_WBITS)
                except zlib.error:
                    pass

        texto = _texto_de_stream_pdf(stream)
        if texto:
            trechos.append(texto)

    if not trechos:
        # Alguns PDFs armazenam o conteúdo da página sem comprimir.
        texto = _texto_de_stream_pdf(conteudo)
        if texto:
            trechos.append(texto)
    return " ".join(trechos)


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
        return _extrair_texto_pdf(conteudo)

    return ""



def consultar_ia_online(pergunta, fontes):
    carregar_config_local()
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        return "Chave GEMINI_API_KEY não configurada no ambiente ou no arquivo .env."
    fontes = [(nome, texto) for nome, texto in fontes if texto.strip()]
    if not fontes:
        return (
            "Não consegui extrair texto das fontes. O formato .doc antigo não é suportado; "
            "converta o arquivo para .docx ou PDF e tente novamente."
        )

    # Reserva espaço para todas as fontes; assim, um arquivo grande não ocupa
    # sozinho o limite e impede que os documentos seguintes sejam enviados.
    limite_contexto = 24000
    cabecalhos = ["[Fonte: {}]\n".format(nome) for nome, texto in fontes]
    espaco_texto = max(1, limite_contexto - sum(len(cabecalho) for cabecalho in cabecalhos))
    limite_por_fonte = max(1, espaco_texto // len(fontes))
    contexto = "\n\n".join(
        cabecalho + texto[:limite_por_fonte]
        for cabecalho, (nome, texto) in zip(cabecalhos, fontes)
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
                "text": (
                    "Responda em português usando somente as fontes fornecidas. "
                    "Considere todas as fontes, compare-as quando relevante e não invente dados. "
                    "Ao apresentar cada informação, cite o nome do arquivo correspondente "
                    "no formato (Fonte: nome do arquivo). Se as fontes divergirem, informe a divergência."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": "Pergunta: {}\n\nFontes:\n{}".format(pergunta, contexto)}]
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

    arquivos_sem_texto = []
    fontes = []
    if request.is_json:
        dados = request.get_json(silent=True) or {}
        pergunta = (dados.get("pergunta") or dados.get("message") or "").strip()
        contexto = dados.get("contexto", "")
        if isinstance(contexto, str) and contexto.strip():
            fontes.append(("contexto enviado", contexto))
    else:
        pergunta = (request.form.get("pergunta") or request.form.get("message") or "").strip()
        for arquivo in request.files.getlist("files"):
            nome = arquivo.filename or "arquivo sem nome"
            texto = extrair_texto(arquivo)
            if texto.strip():
                fontes.append((nome, texto))
            else:
                arquivos_sem_texto.append(nome)

    if not pergunta:
        return jsonify({"response": "Envie uma pergunta.", "resposta": "Envie uma pergunta."}), 400

    if arquivos_sem_texto:
        resposta = (
            "Não consegui extrair texto de: {}. O formato .doc antigo não é suportado; "
            "salve-o como .docx. PDFs digitalizados também podem exigir OCR. "
            "Confira esses arquivos e envie novamente para que todas as fontes sejam analisadas."
        ).format(", ".join(arquivos_sem_texto))
        return jsonify({"response": resposta, "resposta": resposta}), 400

    resposta = consultar_ia_online(pergunta, fontes)
    return jsonify({"response": resposta, "resposta": resposta})



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
