import os
import socket
import threading
import queue
from datetime import datetime
from tkinter import Tk, Label, Button, Entry, Frame, filedialog, Text, END, messagebox
from tkinter.ttk import Style
from flask import Flask, request, redirect, url_for, send_file, send_from_directory, render_template_string, abort
from waitress import serve
import qrcode
from PIL import Image, ImageTk
import zipfile
import io
import urllib.parse

CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB
DEFAULT_PORT = 8000

# ---------- Utilities ----------
def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except:
        return "127.0.0.1"

# ---------- Upload Server ----------
def create_upload_app(upload_folder, log_queue):
    app = Flask(__name__)
    os.makedirs(upload_folder, exist_ok=True)
    app.config['UPLOAD_FOLDER'] = upload_folder
    app.secret_key = "scarsec-upload"

    HTML = """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>LAN-VAULT Created By Shahid Khan</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body{font-family:Arial,sans-serif;background:#f5f7fa;margin:0;padding:0;}
            header{background:#0b74de;color:white;padding:20px;text-align:center;}
            h1{margin:0;font-size:22px;}
            .container{max-width:900px;margin:20px auto;padding:20px;background:white;border-radius:10px;box-shadow:0 6px 18px rgba(0,0,0,0.1);}
            .upload-form{border:2px dashed #0b74de;padding:20px;text-align:center;border-radius:10px;}
            input[type=file]{margin:10px 0;}
            button{background:#0b74de;color:white;border:none;padding:10px 16px;border-radius:8px;font-size:14px;cursor:pointer;}
            button:hover{background:#065cb3;}
            .file-list{margin-top:20px;}
            .file-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;}
            a.link{color:#0b74de;text-decoration:none;}
            footer{text-align:center;margin-top:20px;color:#666;font-size:13px;}
        </style>
    </head>
    <body>
        <header><h1>LAN-VAULT Created By Shahid Khan</h1></header>
        <div class="container">
            <div class="upload-form">
                <input type="file" id="fileInput" multiple>
                <div><button onclick="uploadFiles()">Upload</button></div>
                <div id="status" style="margin-top:5px;font-size:12px;color:#666;">Streamed upload, no size limit</div>
                <progress id="progressBar" value="0" max="100" style="width:100%;display:none;"></progress>
            </div>
            <div class="file-list">
                <h3>Uploaded Files</h3>
                {% if files %}
                    {% for f in files %}
                    <div class="file-item">
                        <div style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{{ f }}</div>
                        <div>
                            <a class="link" href="{{ url_for('download_file', filename=f) }}">Download</a> ·
                            <a class="link" href="{{ url_for('delete_file', filename=f) }}" onclick="return confirm('Delete {{ f }} ?');">Delete</a>
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <div style="font-size:13px;color:#666;">No files uploaded yet.</div>
                {% endif %}
            </div>
        </div>
        <footer>Created by Shahid Khan</footer>
        <script>
        async function uploadFiles(){
            const input = document.getElementById('fileInput');
            const progress = document.getElementById('progressBar');
            progress.style.display='block';
            for(const file of input.files){
                const chunkSize = 100*1024*1024;
                const totalChunks = Math.ceil(file.size/chunkSize);
                for(let i=0;i<totalChunks;i++){
                    const start=i*chunkSize;
                    const end=Math.min(start+chunkSize,file.size);
                    const chunk=file.slice(start,end);
                    const formData=new FormData();
                    formData.append('file',chunk);
                    formData.append('filename',file.name);
                    formData.append('index',i);
                    formData.append('total',totalChunks);
                    await fetch('/upload_chunk',{method:'POST',body:formData});
                    progress.value=((i+1)/totalChunks)*100;
                }
            }
            progress.value=0;
            window.location.reload();
        }
        </script>
    </body>
    </html>
    """

    @app.route('/')
    def index():
        files = sorted(os.listdir(app.config['UPLOAD_FOLDER']))
        return render_template_string(HTML, files=files)

    @app.route('/upload_chunk', methods=['POST'])
    def upload_chunk():
        f = request.files['file']
        filename = request.form['filename']
        index = int(request.form['index'])
        total = int(request.form['total'])
        safe_name = filename.replace('/', '_').replace('\\','_')
        temp_file = os.path.join(app.config['UPLOAD_FOLDER'], safe_name + ".part")

        with open(temp_file,'ab') as wf:
            wf.write(f.read())
            wf.flush()
            os.fsync(wf.fileno())

        if index == total - 1:
            final_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(temp_file, final_path)
            log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] Saved: {safe_name}")
        return 'OK'

    @app.route('/files/<path:filename>')
    def download_file(filename):
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

    @app.route('/delete/<path:filename>')
    def delete_file(filename):
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(path):
            os.remove(path)
            log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] Deleted: {filename}")
        return redirect(url_for('index'))

    return app

# ---------- Download Server ----------
def create_download_app(shared_folder, log_queue):
    app = Flask(__name__)
    os.makedirs(shared_folder, exist_ok=True)
    app.secret_key = "lanvault-download"

    HTML = """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>LAN-VAULT Created By Shahid Khan</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body{font-family:Arial,sans-serif;background:#f5f7fa;margin:0;padding:0;}
            header{background:#0b74de;color:white;padding:20px;text-align:center;}
            h1{margin:0;font-size:22px;}
            .container{max-width:900px;margin:20px auto;padding:20px;background:white;border-radius:10px;box-shadow:0 6px 18px rgba(0,0,0,0.1);}
            .file-list{margin-top:20px;}
            .file-item{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;}
            a.link{color:#0b74de;text-decoration:none;}
            footer{text-align:center;margin-top:20px;color:#666;font-size:13px;}
        </style>
    </head>
    <body>
        <header><h1>LAN-VAULT Created By Shahid Khan</h1></header>
        <div class="container">
            <div class="file-list">
                <h3>Current Path: {{ current_path }}</h3>
                {% if parent_path %}
                <div class="file-item">
                    <div><a class="link" href="{{ url_for('browse', subpath=parent_path) }}">⬅ Back</a></div>
                </div>
                {% endif %}
                {% for name, is_dir, rel_path in items %}
                <div class="file-item">
                    <div style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                        {% if is_dir %}📁 <a class="link" href="{{ url_for('browse', subpath=rel_path) }}">{{ name }}</a>{% else %}📄 {{ name }}{% endif %}
                    </div>
                    <div>
                        {% if is_dir %}
                        <a class="link" href="{{ url_for('download_folder', folderpath=rel_path) }}">Download Folder</a>
                        {% else %}
                        <a class="link" href="{{ url_for('download_file', filepath=rel_path) }}">Download</a>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        <footer>Created by Shahid Khan</footer>
    </body>
    </html>
    """

    def secure_path(path):
        final = os.path.realpath(os.path.join(shared_folder, path))
        if not final.startswith(os.path.realpath(shared_folder)):
            abort(403)
        return final

    @app.route('/', defaults={'subpath': ''})
    @app.route('/browse/<path:subpath>')
    def browse(subpath):
        abs_path = secure_path(subpath)
        current_path = os.path.relpath(abs_path, shared_folder)
        current_path = "" if current_path == "." else current_path
        parent_path = os.path.relpath(os.path.join(abs_path, '..'), shared_folder)
        if parent_path == ".": parent_path = None
        items = []
        for entry in sorted(os.listdir(abs_path)):
            entry_path = os.path.join(abs_path, entry)
            rel_path = os.path.relpath(entry_path, shared_folder)
            rel_path = urllib.parse.quote(rel_path.replace("\\","/"))
            items.append((entry, os.path.isdir(entry_path), rel_path))
        return render_template_string(HTML, items=items, current_path=current_path,
                                      parent_path=urllib.parse.quote(parent_path.replace("\\","/")) if parent_path else None)

    @app.route('/download/file/<path:filepath>')
    def download_file(filepath):
        filepath = urllib.parse.unquote(filepath)
        abs_path = secure_path(filepath)
        if not os.path.isfile(abs_path):
            abort(404)
        log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] Downloaded file: {filepath}")
        return send_file(abs_path, as_attachment=True)

    @app.route('/download/folder/<path:folderpath>')
    def download_folder(folderpath):
        folderpath = urllib.parse.unquote(folderpath)
        abs_path = secure_path(folderpath)
        if not os.path.isdir(abs_path):
            abort(404)
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file,'w',zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(abs_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, abs_path)
                    zf.write(file_path, arcname)
        memory_file.seek(0)
        zip_name = os.path.basename(abs_path.rstrip('/\\'))+".zip"
        log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] Downloaded folder: {folderpath}")
        return send_file(memory_file, download_name=zip_name, as_attachment=True)

    return app

# ---------- Server Thread ----------
class ServerThread(threading.Thread):
    def __init__(self, app, host, port, log_queue):
        threading.Thread.__init__(self)
        self.app = app
        self.host = host
        self.port = port
        self.log_queue = log_queue
        self.daemon = True
    def run(self):
        serve(self.app, host=self.host, port=self.port)

# ---------- GUI ----------
class UnifiedGUI:
    def __init__(self, root):
        self.root = root
        root.title("LAN-VAULT Unified Server")
        self.log_queue = queue.Queue()
        self.server_thread = None
        self.flask_app = None
        self.port = DEFAULT_PORT
        self.build_gui()

    def build_gui(self):
        Label(self.root,text="LAN-VAULT Unified Server",font=("Arial",16)).pack(pady=15)
        Button(self.root,text="Start Upload Server",width=25,command=self.start_upload).pack(pady=10)
        Button(self.root,text="Start Download Server",width=25,command=self.start_download).pack(pady=10)
        Label(self.root,text="Server URL:").pack()
        self.url_entry = Entry(self.root,width=50)
        self.url_entry.pack(pady=5)
        self.qr_label = Label(self.root)
        self.qr_label.pack(pady=10)
        Label(self.root,text="Server Log:").pack()
        self.log_text = Text(self.root,width=70,height=12,state='disabled',wrap='none')
        self.log_text.pack()
        self.stop_btn = Button(self.root,text="Stop Server",width=20,state='disabled',command=self.stop_server)
        self.stop_btn.pack(pady=10)
        self.root.after(200,self.poll_log_queue)

    def start_upload(self):
        folder = filedialog.askdirectory(title="Select folder to save uploads")
        if not folder: return
        self.flask_app = create_upload_app(folder, self.log_queue)
        self.start_server()

    def start_download(self):
        folder = filedialog.askdirectory(title="Select folder to share")
        if not folder: return
        self.flask_app = create_download_app(folder, self.log_queue)
        self.start_server()

    def start_server(self):
        self.server_thread = ServerThread(self.flask_app,"0.0.0.0",self.port,self.log_queue)
        self.server_thread.start()
        ip = get_local_ip()
        url = f"http://{ip}:{self.port}/"
        self.url_entry.delete(0,END)
        self.url_entry.insert(0,url)
        qr = qrcode.QRCode(box_size=6,border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black",back_color="white").resize((150,150))
        self.qr_label.image = ImageTk.PhotoImage(img)
        self.qr_label.config(image=self.qr_label.image)
        self.stop_btn.config(state='normal')
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] Server running at {url}")

    def stop_server(self):
        if messagebox.askyesno("Stop Server","Are you sure you want to stop the server?"):
            os._exit(0)

    def poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            ts = datetime.now().strftime('%H:%M:%S')
            self.log_text.config(state='normal')
            self.log_text.insert(END,f"[{ts}] {msg}\n")
            self.log_text.see(END)
            self.log_text.config(state='disabled')
        self.root.after(200,self.poll_log_queue)

# ---------- Main ----------
def main():
    root = Tk()
    gui = UnifiedGUI(root)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()

if __name__=="__main__":
    main()
