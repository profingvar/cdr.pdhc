"""Frontend views for cdr.pdhc."""
import os
from flask import Blueprint, render_template, send_from_directory

bp = Blueprint("views", __name__)

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'docs')


@bp.get("/")
def landing():
    return render_template("dashboard.html")


@bp.get("/docs")
def docs_index():
    docs = []
    abs_dir = os.path.abspath(DOCS_DIR)
    if os.path.isdir(abs_dir):
        for f in sorted(os.listdir(abs_dir)):
            if f.endswith('.md'):
                title = f.replace('.md', '').replace('_', ' ').replace('-', ' ').title()
                docs.append({"filename": f, "title": title})
    return render_template("docs.html", docs=docs)


@bp.get("/docs/download/<filename>")
def download_doc(filename):
    abs_dir = os.path.abspath(DOCS_DIR)
    if not filename.endswith('.md'):
        return 'Not found', 404
    return send_from_directory(abs_dir, filename, as_attachment=True)
