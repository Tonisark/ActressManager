## README.md (summary)

Use the provided `app.py`. Put your `actresses.db` in the project root (or let the app create it). Put per-actress media folders under `media/<FolderName>/thumbnail.jpg` where `FolderName` matches `folder_name` column in the DB (we use `folder_name` to map media).

Run:

```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
FLASK_APP=app.py flask run
# or: python app.py
```

Open http://127.0.0.1:5000

---