from pathlib import Path
import sys

ROOT = Path.cwd()
MAIN = ROOT / "main.py"
HTML = ROOT / "index.html"


def replace_once(content: str, old: str, new: str, description: str) -> str:
    if old in content:
        return content.replace(old, new, 1)
    if new in content:
        return content
    raise RuntimeError(
        f"Não encontrei o trecho esperado para: {description}. "
        "Seu arquivo pode ter sido alterado; nenhuma substituição parcial adicional foi feita."
    )


def main() -> int:
    missing = [p.name for p in (MAIN, HTML) if not p.is_file()]
    if missing:
        print("Execute este script na pasta raiz do Baby_Yoda_I.A.")
        print("Arquivos não encontrados: " + ", ".join(missing))
        return 1

    for asset in ("logo-yoda.png", "marca-agua.png"):
        if not (ROOT / asset).is_file():
            print(f"Atenção: não encontrei {asset}; confira se a imagem está no repositório.")

    main_text = MAIN.read_text(encoding="utf-8")
    html_text = HTML.read_text(encoding="utf-8")

    # Faz cópias de segurança antes de alterar os dois arquivos existentes.
    if not (ROOT / "main.py.render-backup").exists():
        (ROOT / "main.py.render-backup").write_text(main_text, encoding="utf-8")
    if not (ROOT / "index.html.render-backup").exists():
        (ROOT / "index.html.render-backup").write_text(html_text, encoding="utf-8")

    main_text = replace_once(
        main_text,
        "from flask import Flask, jsonify, request",
        "from flask import Flask, jsonify, request, send_from_directory",
        "importar send_from_directory do Flask",
    )

    main_text = replace_once(
        main_text,
        'app = Flask(__name__)\napp.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024\n',
        '''app = Flask(__name__)\napp.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024\nBASE_DIR = os.path.dirname(os.path.abspath(__file__))\n\n\n# A interface e a API são servidas pelo mesmo serviço no Render.\n@app.get("/")\ndef home():\n    return send_from_directory(BASE_DIR, "index.html")\n\n\n# Servimos somente as duas imagens públicas usadas pela interface.\n@app.get("/logo-yoda.png")\ndef logo_yoda():\n    return send_from_directory(BASE_DIR, "logo-yoda.png")\n\n\n@app.get("/marca-agua.png")\ndef marca_agua():\n    return send_from_directory(BASE_DIR, "marca-agua.png")\n\n\n@app.get("/healthz")\ndef healthz():\n    return jsonify({"status": "ok"})\n''',
        "rotas da página, imagens e health check",
    )

    # Atualiza o padrão para o modelo Flash estável atualmente documentado.
    main_text = main_text.replace(
        'MODELS = [os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()]',
        'MODELS = [os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()]',
    )
    main_text = main_text.replace(
        'or ["gemini-2.5-flash"]',
        'or ["gemini-3.8-flash"]',
    )

    main_text = replace_once(
        main_text,
        "        url = \"https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}\".format(\n            urllib.parse.quote(model, safe=\"\"), urllib.parse.quote(API_KEY, safe=\"\")\n        )\n",
        "        url = \"https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent\".format(\n            urllib.parse.quote(model, safe=\"\")\n        )\n",
        "não enviar a chave da Gemini na URL",
    )

    main_text = replace_once(
        main_text,
        '            headers={"Content-Type": "application/json"},\n',
        '            headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},\n',
        "enviar a chave da Gemini no cabeçalho HTTP",
    )

    cors_block = '''@app.after_request\ndef add_cors_headers(response):\n    response.headers["Access-Control-Allow-Origin"] = "*"\n    response.headers["Access-Control-Allow-Headers"] = "Content-Type"\n    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"\n    return response\n\n@app.route("/perguntar", methods=["POST", "OPTIONS"])\ndef perguntar():\n    if request.method == "OPTIONS":\n        return ("", 204)\n'''
    same_origin_block = '''@app.route("/perguntar", methods=["POST"])\ndef perguntar():\n'''
    main_text = replace_once(
        main_text,
        cors_block,
        same_origin_block,
        "remover CORS aberto e manter a API no mesmo domínio da interface",
    )

    main_text = replace_once(
        main_text,
        'app.run(host="0.0.0.0", port=5000, debug=False)',
        'app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)',
        "usar a porta definida pelo ambiente",
    )

    html_text = replace_once(
        html_text,
        'const API_URL = "http://localhost:5000/perguntar";',
        'const API_URL = "/perguntar";',
        "fazer o navegador chamar a API no mesmo domínio",
    )
    html_text = html_text.replace(
        '"Verifique se o backend está ligado, aceita multipart/form-data com os campos message e files, e permite CORS.";',
        '"Confira se o serviço está ativo e se a rota /perguntar está disponível.";',
    )

    MAIN.write_text(main_text, encoding="utf-8")
    HTML.write_text(html_text, encoding="utf-8")

    print("Preparação concluída.")
    print("- main.py: serve a página, as imagens, o health check e a API; lê PORT; usa Gemini 3.8 Flash por padrão.")
    print("- main.py: envia GEMINI_API_KEY no cabeçalho HTTP, não na URL.")
    print("- index.html: chama /perguntar no mesmo domínio do site.")
    print("- requirements.txt, .gitignore, .env.example e render.yaml: arquivos de deploy incluídos no pacote.")
    print("\nAntes do deploy: retire o .env do controle do Git com `git rm --cached .env` (se estiver rastreado).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        raise SystemExit(1)
