from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel
from typing import List
from google import genai
import uvicorn
import pdfplumber
import pandas as pd
import json
import os
from dotenv import load_dotenv
import io
import calendar
from collections import defaultdict
from fpdf import FPDF
import tempfile
from functools import lru_cache
import hashlib
import asyncio

# 1. Cargamos tu clave
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")

try:
    mi_cliente = genai.Client(api_key=API_KEY)
except Exception as e:
    print("⚠️ Error: No se pudo conectar con Gemini.")

# 2. Levantamos el servidor
app = FastAPI(title="API Cuando Rindo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS DE DATOS ---
class Evento(BaseModel):
    fecha: str
    materia: str
    tipo: str

class PedidoPDF(BaseModel):
    eventos: List[Evento]

# --- CONSTANTES DEL DISEÑO PREMIUM ---
MONTH_NAMES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

COLORS = {
    "parcial":       {"bg": (234, 243, 222), "text": (59, 109, 17)},
    "recuperatorio": {"bg": (250, 238, 218), "text": (133, 79, 11)},
    "final":         {"bg": (252, 235, 235), "text": (163, 45, 45)},
    "actividad":     {"bg": (230, 241, 251), "text": (24, 95, 165)},
}

LEGEND_ITEMS = [
    ("Parcial",        "parcial"),
    ("Recuperatorio",  "recuperatorio"),
    ("Final",          "final"),
    ("Actividad / TP", "actividad"),
]

def classify(label: str) -> str:
    l = label.lower()
    if "recuperatorio" in l: return "recuperatorio"
    if "final" in l: return "final"
    if "parcial" in l: return "parcial"
    if "actividad" in l or " tp" in l or "entrega" in l or "obligatoria" in l: return "actividad"
    return "parcial"

# Layout params adaptados a Apaisado (Landscape)
MARGIN_LEFT   = 14.0
MARGIN_TOP    = 14.0
MARGIN_RIGHT  = 14.0
USABLE_W      = 297 - MARGIN_LEFT - MARGIN_RIGHT   
COL_W         = USABLE_W / 7                       
HEADER_H      = 14.0
DAY_NAME_H    = 7.0
LEGEND_H      = 9.0
PILL_H        = 4.5
PILL_MARGIN   = 1.0
CORNER_R      = 1.5

COLOR_BORDER  = (210, 210, 210)
COLOR_BG_DAY  = (255, 255, 255)
COLOR_BG_EMPTY= (248, 248, 248)
COLOR_DAY_NUM = (100, 100, 100)
COLOR_DAY_NUM_EVENT = (30, 30, 30)
COLOR_DAY_HEADER_BG = (245, 245, 245)
COLOR_MONTH_TEXT = (20, 20, 20)
COLOR_YEAR_TEXT  = (130, 130, 130)

class CalendarPDF(FPDF):
    def _set_fill(self, rgb): self.set_fill_color(*rgb)
    def _set_text(self, rgb): self.set_text_color(*rgb)
    def _set_draw(self, rgb): self.set_draw_color(*rgb)

    def draw_month(self, year: int, month: int, events_by_day: dict):
        self.add_page()
        x0, y0 = MARGIN_LEFT, MARGIN_TOP

        # Cabecera
        self._set_text(COLOR_MONTH_TEXT)
        self.set_font("Arial", "B", 16) 
        month_label = MONTH_NAMES[month]
        ancho_mes = self.get_string_width(month_label) # <-- Medimos ANTES de achicar la letra
        
        # --- EL ARREGLO DE ALINEACIÓN ---
        linea_base = y0 + 10 # Definimos la línea base imaginaria
        
        # Imprimimos el Mes
        self.set_xy(x0, y0)
        self.text(x0, linea_base, month_label)
        
        # Imprimimos el Año al lado, apoyado en la misma línea
        self.set_font("Arial", "", 11)
        self._set_text(COLOR_YEAR_TEXT)
        self.text(x0 + ancho_mes + 2, linea_base, str(year))
        y0 += HEADER_H

        # Leyenda
        lx = x0
        for label, key in LEGEND_ITEMS:
            c = COLORS[key]
            dot_size = 3.5
            self._set_fill(c["bg"])
            self._set_draw(c["bg"])
            self.rect(lx, y0 + (LEGEND_H - dot_size) / 2, dot_size, dot_size, "F")
            self._set_draw(c["text"])
            self.set_line_width(0.2)
            self.rect(lx, y0 + (LEGEND_H - dot_size) / 2, dot_size, dot_size, "D")
            self.set_line_width(0.1)
            self._set_text(COLOR_YEAR_TEXT)
            self.set_font("Arial", "", 7.5)
            self.set_xy(lx + dot_size + 1.5, y0 + (LEGEND_H - 3.5) / 2)
            self.cell(0, 3.5, label, ln=0)
            lx += dot_size + 1.5 + self.get_string_width(label) + 6
        y0 += LEGEND_H

        # Nombres de días
        self._set_fill(COLOR_DAY_HEADER_BG)
        self._set_draw(COLOR_BORDER)
        self.set_line_width(0.2)
        for i, name in enumerate(DAY_NAMES):
            cx = x0 + i * COL_W
            self.rect(cx, y0, COL_W, DAY_NAME_H, "F")
            self.set_font("Arial", "B", 7)
            self._set_text(COLOR_YEAR_TEXT)
            self.set_xy(cx, y0)
            self.cell(COL_W, DAY_NAME_H, name, align="C", ln=0)
        y0 += DAY_NAME_H

        # Celdas de días
        weeks = calendar.monthcalendar(year, month)
        usable_h = 210 - y0 - MARGIN_TOP
        actual_row_h = usable_h / len(weeks)

        for week in weeks:
            for col, day in enumerate(week):
                cx = x0 + col * COL_W
                cy = y0
                is_empty = (day == 0)

                self._set_fill(COLOR_BG_EMPTY if is_empty else COLOR_BG_DAY)
                self._set_draw(COLOR_BORDER)
                self.set_line_width(0.2)
                self.rect(cx, cy, COL_W, actual_row_h, "FD")

                if not is_empty:
                    day_events = events_by_day.get(day, [])
                    self.set_font("Arial", "B" if day_events else "", 8)
                    self._set_text(COLOR_DAY_NUM_EVENT if day_events else COLOR_DAY_NUM)
                    self.set_xy(cx + 2, cy + 2)
                    self.cell(COL_W - 4, 4, str(day), align="L", ln=0)

                    py = cy + 7
                    for ev in day_events:
                        etype = classify(ev["event"])
                        c = COLORS[etype]
                        
                        texto_original = f"{ev['subject']} - {ev['event']}"
                        self.set_font("Arial", "", 8) 
                        max_w = COL_W - 4
                        
                        # LOGICA DE PASTILLA ELÁSTICA (1 o 2 Renglones)
                        if self.get_string_width(texto_original) <= max_w:
                            # Entra perfecto en 1 renglón
                            pill_h_actual = PILL_H
                            self._set_fill(c["bg"])
                            self._set_draw(c["bg"])
                            self.set_line_width(0.0)
                            self.rect(cx + 1.5, py, COL_W - 3, pill_h_actual, "F")
                            self._set_text(c["text"])
                            self.set_xy(cx + 2, py + 0.5)
                            self.cell(max_w, pill_h_actual - 1, texto_original, ln=0)
                        else:
                            # Es largo, usamos 2 renglones
                            pill_h_actual = PILL_H * 1.8 # Hacemos la pastilla casi el doble de alta
                            self._set_fill(c["bg"])
                            self._set_draw(c["bg"])
                            self.set_line_width(0.0)
                            self.rect(cx + 1.5, py, COL_W - 3, pill_h_actual, "F")
                            self._set_text(c["text"])
                            
                            # Separamos el texto inteligentemente en dos líneas
                            palabras = texto_original.split()
                            linea1, linea2 = "", ""
                            for palabra in palabras:
                                if self.get_string_width(linea1 + palabra + " ") <= max_w:
                                    linea1 += palabra + " "
                                elif self.get_string_width(linea2 + palabra + " ") <= max_w - 2: 
                                    linea2 += palabra + " "
                                else:
                                    linea2 = linea2.strip() + "..."
                                    break
                                    
                            self.set_xy(cx + 2, py + 0.5)
                            self.cell(max_w, (pill_h_actual / 2), linea1.strip(), ln=2)
                            self.set_x(cx + 2)
                            self.cell(max_w, (pill_h_actual / 2) - 1, linea2.strip(), ln=0)

                        py += pill_h_actual + PILL_MARGIN

                        if py + pill_h_actual > cy + actual_row_h - 1:
                            break
            y0 += actual_row_h

        # --- NUEVO CÓDIGO: MARCA DE AGUA AL PIE ---
        # Nos posicionamos a 10 mm del borde inferior (A4 apaisado tiene 210mm de alto)
        self.set_y(200) 
        self.set_font("Arial", "", 8)
        self._set_text(COLOR_YEAR_TEXT) # Usa exactamente el mismo gris del "2026"
        self.cell(0, 5, "Generado con @cuando.rindo", align="C", ln=0)
# --- MANEJO DE CACHE EN DISCO ---
CACHE_FILE = "cache_gemini.json"

def cargar_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def guardar_cache(cache_data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

def generar_hash(texto):
    # Crea un identificador único (código de barras) para el texto exacto
    return hashlib.md5(texto.encode('utf-8')).hexdigest()

# --- IA Y EXTRACCIÓN ---
def extraer_datos_con_ia(texto_completo):
    prompt = f"""
    Actúa como un asistente universitario experto. Analiza el siguiente texto que contiene cronogramas de VARIAS materias y extrae la información.
    
    REGLAS ESTRICTAS:
    1. Identifica el Nombre de la Asignatura COMPLETO (ej: "Finanzas Computacionales", "Análisis Matemático II"). No lo acortes.
    2. Extrae los exámenes principales: Parciales, Finales y Recuperatorios.
       - REGLA DE COMBINACIÓN: Si coinciden Recuperatorio y Final DE LA MISMA ASIGNATURA, combínalos en: "Recuperatorio / Final".
    3. ATENCIÓN ESPECIAL: Extrae también CUALQUIER "Actividad Obligatoria", "Trabajo Práctico", "Control de Lectura", "Entrega", "Caso de Estudio". 
    4. IGNORA: Feriados, clases teóricas normales, clases de consulta. 
    5. Formato de fecha obligatorio: DD/MM (ejemplo: 07/08).
    6. SÍNTESIS EXTREMA PARA LA DESCRIPCIÓN: Usa ÚNICAMENTE: "1er Parcial", "2do Parcial", "Final", "Recuperatorio", "Recuperatorio / Final" o "Actividad Obligatoria".
    
    Responde ÚNICA Y EXCLUSIVAMENTE con un JSON válido usando esta estructura exacta (un array plano):
    [
        {{"materia": "Nombre de la materia 1", "fecha": "DD/MM", "tipo": "Nombre del evento"}},
        {{"materia": "Nombre de la materia 2", "fecha": "DD/MM", "tipo": "Nombre del evento"}}
    ]

    TEXTO A ANALIZAR:
    {texto_completo}
    """
    try:
        response = mi_cliente.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Error con la IA: {e}")
        return None
async def procesar_con_ia_async(texto):
    # Esto envuelve tu función original para que corra en un hilo separado
    # y permita procesar múltiples PDFs al mismo tiempo.
    return await asyncio.to_thread(extraer_datos_con_ia, texto)

# --- RUTAS ---
# Esta ruta sirve para que cuando alguien entre al link, vea tu diseño
@app.get("/")
async def mostrar_interfaz():
    return FileResponse("index.html")
@app.post("/generar-calendario")
async def generar_calendario(archivos: list[UploadFile] = File(...)):
    eventos_globales = []
    tareas_ia_pendientes = []
    hashes_pendientes = []
    
    # 1. Cargamos el disco duro
    cache_actual = cargar_cache()

    for archivo in archivos:
        nombre_archivo = archivo.filename.lower()
        texto_bruto = ""
        contenido_bytes = await archivo.read()
        
        # Extracción de texto
        try:
            if nombre_archivo.endswith('.pdf'):
                with pdfplumber.open(io.BytesIO(contenido_bytes)) as pdf:
                    texto_bruto = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
            elif nombre_archivo.endswith('.xlsx'):
                df = pd.read_excel(io.BytesIO(contenido_bytes), header=None, dtype=str)
                df = df.fillna('')
                texto_bruto = '\n'.join(df.apply(lambda row: ' '.join(row), axis=1))
            elif nombre_archivo.endswith('.csv'):
                df = pd.read_csv(io.BytesIO(contenido_bytes), header=None, dtype=str)
                df = df.fillna('')
                texto_bruto = '\n'.join(df.apply(lambda row: ' '.join(row), axis=1))
        except Exception as e:
            print(f"Error leyendo {nombre_archivo}: {e}")
            continue
                
        if texto_bruto.strip():
            # 2. Le creamos el código de barras al texto
            hash_texto = generar_hash(texto_bruto)
            
            # 3. Chequeamos si ya lo conocemos
            if hash_texto in cache_actual:
                print(f"⚡ CACHÉ HIT: El archivo {nombre_archivo} ya fue procesado antes. Recuperando al instante.")
                eventos_globales.extend(cache_actual[hash_texto])
            else:
                # 4. Es nuevo. Lo preparamos para mandarlo a Google
                print(f"⏳ NUEVO: Preparando {nombre_archivo} para enviar a Gemini.")
                texto_para_ia = f"--- INICIO DEL ARCHIVO: {archivo.filename} ---\n{texto_bruto}\n--- FIN DEL ARCHIVO ---"
                
                tareas_ia_pendientes.append(procesar_con_ia_async(texto_para_ia))
                hashes_pendientes.append(hash_texto)

    # 5. Si hay archivos nuevos, los procesamos TODOS JUNTOS EN PARALELO
    if tareas_ia_pendientes:
        print(f"🚀 Enviando {len(tareas_ia_pendientes)} archivos en paralelo a Gemini...")
        
        # Magia negra de Python: espera que terminen todas juntas
        resultados_paralelos = await asyncio.gather(*tareas_ia_pendientes)
        
        hubo_cambios = False
        for i, resultado in enumerate(resultados_paralelos):
            if resultado and isinstance(resultado, list):
                # Lo sumamos a la respuesta final
                eventos_globales.extend(resultado)
                # Lo guardamos en memoria
                hash_correspondiente = hashes_pendientes[i]
                cache_actual[hash_correspondiente] = resultado
                hubo_cambios = True
                
        # 6. Si aprendimos algo nuevo, actualizamos el archivo físico
        if hubo_cambios:
            guardar_cache(cache_actual)
            print("💾 Caché físico actualizado exitosamente.")

    return {"estado": "exito", "resultados": eventos_globales}

@app.post("/generar-pdf")
async def generar_pdf(pedido: PedidoPDF):
    año_actual = 2026
    months_needed = {}
    events_map = defaultdict(lambda: defaultdict(list))
    
    for ev in pedido.eventos:
        try:
            dia_str, mes_str = ev.fecha.split('/')
            dia, mes = int(dia_str), int(mes_str)
            key = (año_actual, mes)
            months_needed[key] = True
            events_map[key][dia].append({"subject": ev.materia, "event": ev.tipo})
        except Exception as e:
            continue
            
    sorted_months = sorted(months_needed.keys())
    
    pdf = CalendarPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(0, 0, 0)
    
    for (year, month) in sorted_months:
        pdf.draw_month(year, month, events_map[(year, month)])
        
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf.output(tmp.name)
        with open(tmp.name, "rb") as f:
            pdf_bytes = f.read()
            
    return Response(content=pdf_bytes, media_type="application/pdf")

if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)