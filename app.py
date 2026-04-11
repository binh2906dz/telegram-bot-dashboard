from flask import Flask, render_template, request, redirect, url_for
import os, json, threading
from bot import run_bot

app = Flask(__name__)
UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= JSON =================
def load(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except:
        return default

def save(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=2)

# ================= HOME =================
@app.route("/")
def home():
    albums = load("albums.json", {})
    subs = load("subscribers.json", [])
    return render_template("index.html", albums=albums, subs=subs)

# ================= UPLOAD =================
@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("images")
    captions = request.form.getlist("captions")

    albums = load("albums.json", {})

    album_id = request.form.get("album_id")
    if not album_id:
        return redirect("/")

    if album_id not in albums:
        albums[album_id] = {"photos": []}

    for i, file in enumerate(files):
        if file:
            path = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(path)

            caption = captions[i] if i < len(captions) else ""

            albums[album_id]["photos"].append({
                "url": f"/{path}",
                "caption": caption
            })

    save("albums.json", albums)
    return redirect("/")

# ================= DELETE =================
@app.route("/delete/<aid>")
def delete(aid):
    albums = load("albums.json", {})
    if aid in albums:
        del albums[aid]
        save("albums.json", albums)
    return redirect("/")

# ================= RUN BOT =================
def start_bot():
    run_bot()

if __name__ == "__main__":
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=5000)
