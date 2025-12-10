import os
import shutil
import uuid
import zipfile
from typing import List

import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfWriter, PdfReader
import img2pdf

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- CONFIGURACIÓN Y SEGURIDAD ---
UPLOAD_DIR = "temp_uploads"
OUTPUT_DIR = "temp_outputs"
MAX_MB_PER_FILE = 30  # Límite de 20MB por archivo para evitar colapsos
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

# Crear carpetas si no existen
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- FUNCIONES DE AYUDA Y SEGURIDAD ---

def limpiar_inicio(dir_path):
    """Borra archivos viejos al arrancar el servidor."""
    for filename in os.listdir(dir_path):
        file_path = os.path.join(dir_path, filename)
        try:
            if os.path.isfile(file_path):
                os.unlink(file_path)
        except Exception:
            pass

# Limpieza inicial al ejecutar el script
limpiar_inicio(UPLOAD_DIR)
limpiar_inicio(OUTPUT_DIR)

def validar_extension(filename: str, permitidas=ALLOWED_EXTENSIONS):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in permitidas:
        raise HTTPException(status_code=400, detail=f"Extensión no permitida: {ext}")
    return ext

def borrar_archivos(file_paths: List[str]):
    """Tarea en segundo plano para limpiar archivos después de servir la respuesta."""
    for path in file_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"🧹 Limpiado: {path}")
        except Exception as e:
            print(f"Error borrando {path}: {e}")

def guardar_con_limite(upload_file: UploadFile, destino_path: str, max_mb: int):
    """
    Guarda el archivo en bloques. Si supera max_mb, aborta y borra.
    Evita que la RAM o el Disco se llenen con archivos gigantes.
    """
    limit_bytes = max_mb * 1024 * 1024
    file_size = 0
    
    with open(destino_path, "wb") as buffer:
        while True:
            chunk = upload_file.file.read(1024 * 1024) # Leer 1MB a la vez
            if not chunk:
                break
            file_size += len(chunk)
            if file_size > limit_bytes:
                buffer.close()
                if os.path.exists(destino_path):
                    os.remove(destino_path)
                raise HTTPException(status_code=413, detail=f"El archivo excede el límite de {max_mb}MB.")
            buffer.write(chunk)

# --- RUTAS ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# 1. UNIR PDFS
@app.post("/api/unir")
async def api_unir(files: List[UploadFile], background_tasks: BackgroundTasks):
    if not files:
        return JSONResponse(status_code=400, content={"message": "No enviaste archivos."})
    
    merger = PdfWriter()
    temp_paths = []
    output_filename = f"Unido_{uuid.uuid4().hex}.pdf"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    try:
        for file in files:
            validar_extension(file.filename, {".pdf"})
            
            safe_filename = f"{uuid.uuid4().hex}.pdf"
            safe_path = os.path.join(UPLOAD_DIR, safe_filename)
            
            guardar_con_limite(file, safe_path, MAX_MB_PER_FILE)
            temp_paths.append(safe_path)
            merger.append(safe_path)
        
        merger.write(output_path)
        merger.close()
        
        background_tasks.add_task(borrar_archivos, temp_paths + [output_path])
        return FileResponse(output_path, filename="PDFlex_Unido.pdf", headers={"X-Success-Message": "Unión exitosa"})

    except Exception as e:
        borrar_archivos(temp_paths)
        return JSONResponse(status_code=500, content={"message": str(e)})

# 2. COMPRIMIR PDFS
@app.post("/api/comprimir")
async def api_comprimir(files: List[UploadFile], background_tasks: BackgroundTasks):
    if not files:
        return JSONResponse(status_code=400, content={"message": "Falta el archivo."})

    processed_files = []
    temp_inputs = []
    total_orig = 0
    total_comp = 0

    try:
        for file in files:
            validar_extension(file.filename, {".pdf"})
            
            safe_filename = f"{uuid.uuid4().hex}.pdf"
            input_path = os.path.join(UPLOAD_DIR, safe_filename)
            guardar_con_limite(file, input_path, MAX_MB_PER_FILE)
            temp_inputs.append(input_path)
            
            total_orig += os.path.getsize(input_path)

            reader = PdfReader(input_path)
            writer = PdfWriter()
            
            # Compresión de streams
            for page in reader.pages:
                writer.add_page(page)
                writer.pages[-1].compress_content_streams()
            
            output_name = f"Mini_{uuid.uuid4().hex}.pdf"
            output_path = os.path.join(OUTPUT_DIR, output_name)
            
            with open(output_path, "wb") as f:
                writer.write(f)
            
            total_comp += os.path.getsize(output_path)
            processed_files.append(output_path)

        # Preparar descarga (Zip o archivo único)
        if len(processed_files) == 1:
            final_path = processed_files[0]
            download_name = "PDFlex_Comprimido.pdf"
        else:
            final_path = os.path.join(OUTPUT_DIR, f"Pack_{uuid.uuid4().hex}.zip")
            with zipfile.ZipFile(final_path, 'w') as zf:
                for f in processed_files:
                    zf.write(f, os.path.basename(f))
            processed_files.append(final_path) # Agregar zip a la lista de borrado
            download_name = "PDFlex_Pack.zip"

        ahorro = total_orig - total_comp
        porcentaje = int((ahorro / total_orig) * 100) if total_orig > 0 else 0
        
        background_tasks.add_task(borrar_archivos, temp_inputs + processed_files)
        
        headers = {
            "Access-Control-Expose-Headers": "X-Savings-Percent",
            "X-Savings-Percent": str(porcentaje)
        }
        return FileResponse(final_path, filename=download_name, headers=headers)

    except Exception as e:
        borrar_archivos(temp_inputs + processed_files)
        return JSONResponse(status_code=500, content={"message": str(e)})

# 3. IMÁGENES A PDF
@app.post("/api/img2pdf")
async def api_img2pdf(files: List[UploadFile], background_tasks: BackgroundTasks):
    if not files:
        return JSONResponse(status_code=400, content={"message": "No hay imágenes."})
    
    img_paths = []
    output_filename = f"Album_{uuid.uuid4().hex}.pdf"
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    
    try:
        for file in files:
            ext = validar_extension(file.filename, {".jpg", ".jpeg", ".png"})
            
            safe_filename = f"{uuid.uuid4().hex}{ext}"
            path = os.path.join(UPLOAD_DIR, safe_filename)
            guardar_con_limite(file, path, MAX_MB_PER_FILE)
            img_paths.append(path)
        
        pdf_bytes = img2pdf.convert(img_paths)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
            
        background_tasks.add_task(borrar_archivos, img_paths + [output_path])
        return FileResponse(output_path, filename="PDFlex_Album.pdf")

    except Exception as e:
        borrar_archivos(img_paths)
        return JSONResponse(status_code=500, content={"message": str(e)})

# 4. EXTRAER PÁGINAS (SPLIT)
@app.post("/api/extraer")
async def api_extraer(
    file: UploadFile, 
    background_tasks: BackgroundTasks,
    inicio: int = Form(...), 
    fin: int = Form(...),
):
    temp_paths = []
    
    try:
        validar_extension(file.filename, {".pdf"})
        
        safe_filename = f"{uuid.uuid4().hex}.pdf"
        input_path = os.path.join(UPLOAD_DIR, safe_filename)
        guardar_con_limite(file, input_path, MAX_MB_PER_FILE)
        temp_paths.append(input_path)
        
        reader = PdfReader(input_path)
        writer = PdfWriter()
        total_pages = len(reader.pages)
        
        if inicio < 1 or fin > total_pages or inicio > fin:
            raise HTTPException(status_code=400, detail=f"Rango inválido. El PDF tiene {total_pages} páginas.")

        for i in range(inicio - 1, fin):
            writer.add_page(reader.pages[i])
            
        output_filename = f"Extraido_{uuid.uuid4().hex}.pdf"
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        
        with open(output_path, "wb") as f:
            writer.write(f)
            
        background_tasks.add_task(borrar_archivos, temp_paths + [output_path])
        return FileResponse(output_path, filename="PDFlex_Extraido.pdf")

    except Exception as e:
        borrar_archivos(temp_paths)
        return JSONResponse(status_code=500, content={"message": str(e)})

# 5. ROTAR PDF
@app.post("/api/rotar")
async def api_rotar(
    file: UploadFile, 
    background_tasks: BackgroundTasks,
    grados: int = Form(...),
):
    temp_paths = []
    
    try:
        validar_extension(file.filename, {".pdf"})
        
        if grados not in [90, 180, 270]:
            raise HTTPException(status_code=400, detail="Solo se permiten 90, 180 o 270 grados.")

        safe_filename = f"{uuid.uuid4().hex}.pdf"
        input_path = os.path.join(UPLOAD_DIR, safe_filename)
        guardar_con_limite(file, input_path, MAX_MB_PER_FILE)
        temp_paths.append(input_path)
        
        reader = PdfReader(input_path)
        writer = PdfWriter()
        
        for page in reader.pages:
            page.rotate(grados)
            writer.add_page(page)
            
        output_filename = f"Rotado_{uuid.uuid4().hex}.pdf"
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        
        with open(output_path, "wb") as f:
            writer.write(f)
            
        background_tasks.add_task(borrar_archivos, temp_paths + [output_path])
        return FileResponse(output_path, filename="PDFlex_Rotado.pdf")

    except Exception as e:
        borrar_archivos(temp_paths)
        return JSONResponse(status_code=500, content={"message": str(e)})

if __name__ == "__main__":
    print("🚀 PDFlex Seguro iniciando en http://127.0.0.1:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)