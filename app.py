# -*- coding: utf-8 -*-
"""
HidroSed · Módulo Eje Cauce y Secciones v1
Aplicación Streamlit independiente para validar eje de cauce, tramo útil, perfil longitudinal,
secciones transversales y exportaciones KMZ/Excel/CSV/JSON.

Diseñada para integrarse posteriormente en HidroSed.
"""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import zipfile
import requests
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Any
import xml.etree.ElementTree as ET
from html import escape

import numpy as np
import pandas as pd
import streamlit as st
from shapely.geometry import LineString, Point, MultiLineString
from shapely.ops import transform as shp_transform
from pyproj import CRS, Transformer
import matplotlib.pyplot as plt

# Dependencias opcionales controladas
try:
    import rasterio
    from rasterio.transform import rowcol
except Exception:  # pragma: no cover
    rasterio = None

try:
    import ezdxf
except Exception:  # pragma: no cover
    ezdxf = None

try:
    import plotly.graph_objects as go
except Exception:  # pragma: no cover
    go = None


# --------------------------------------------------------------------------------------
# Configuración Streamlit
# --------------------------------------------------------------------------------------
st.set_page_config(
    page_title="HidroSed · Eje Cauce y Secciones",
    page_icon="🌊",
    layout="wide",
)


# --------------------------------------------------------------------------------------
# Modelos de datos
# --------------------------------------------------------------------------------------
@dataclass
class ProfilePoint:
    km: float
    x: float
    y: float
    z_dem: Optional[float] = None
    z_respaldo: Optional[float] = None
    diferencia: Optional[float] = None
    pendiente_local: Optional[float] = None


@dataclass
class CrossSection:
    km: float
    center_x: float
    center_y: float
    left_x: float
    left_y: float
    right_x: float
    right_y: float
    stations: List[float]
    elevations: List[float]
    source: str
    status: str = "aceptada"
    notes: str = ""


@dataclass
class AxisValidation:
    axis_length_m: float
    useful_length_m: float
    km_pc_hidrologico: float
    km_pc_soporte: float
    direction: str
    inverted: bool
    left_right_defined: bool
    warnings: List[str]


# --------------------------------------------------------------------------------------
# Utilidades de archivos KML/KMZ/DXF/Excel
# --------------------------------------------------------------------------------------
def uploaded_bytes(uploaded_file) -> bytes:
    uploaded_file.seek(0)
    return uploaded_file.read()


def read_kml_text_from_upload(uploaded_file) -> str:
    """Lee archivo KML o KMZ y retorna texto KML."""
    data = uploaded_bytes(uploaded_file)
    name = uploaded_file.name.lower()
    if name.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as z:
            kml_names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("El KMZ no contiene archivo .kml interno.")
            # preferir doc.kml si existe
            kml_name = "doc.kml" if "doc.kml" in kml_names else kml_names[0]
            return z.read(kml_name).decode("utf-8", errors="ignore")
    return data.decode("utf-8", errors="ignore")


def parse_kml_coordinates(coord_text: str) -> List[Tuple[float, float, Optional[float]]]:
    coords: List[Tuple[float, float, Optional[float]]] = []
    if not coord_text:
        return coords
    for token in coord_text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                z = float(parts[2]) if len(parts) >= 3 and parts[2] != "" else None
                coords.append((lon, lat, z))
            except ValueError:
                continue
    return coords


def extract_kml_geometries(uploaded_file) -> Dict[str, Any]:
    """Extrae puntos y líneas de KML/KMZ. Coordenadas en lon,lat,z."""
    kml_text = read_kml_text_from_upload(uploaded_file)
    root = ET.fromstring(kml_text.encode("utf-8"))
    ns = {"kml": "http://www.opengis.net/kml/2.2"}

    def findall_any(parent, tag):
        found = parent.findall(f".//kml:{tag}", ns)
        if found:
            return found
        return parent.findall(f".//{tag}")

    points = []
    lines = []
    placemarks = findall_any(root, "Placemark")
    for pm in placemarks:
        name_el = pm.find("kml:name", ns)
        if name_el is None:
            name_el = pm.find("name")
        name = name_el.text.strip() if name_el is not None and name_el.text else "sin_nombre"
        for pt in findall_any(pm, "Point"):
            coord_el = pt.find("kml:coordinates", ns)
            if coord_el is None:
                coord_el = pt.find("coordinates")
            coords = parse_kml_coordinates(coord_el.text if coord_el is not None else "")
            if coords:
                points.append({"name": name, "coords": coords[0]})
        for line in findall_any(pm, "LineString"):
            coord_el = line.find("kml:coordinates", ns)
            if coord_el is None:
                coord_el = line.find("coordinates")
            coords = parse_kml_coordinates(coord_el.text if coord_el is not None else "")
            if len(coords) >= 2:
                lines.append({"name": name, "coords": coords})
        # gx:Track no implementado formalmente, pero se intenta leer gx:coord si existe
        for track in pm.findall(".//{http://www.google.com/kml/ext/2.2}Track"):
            coords = []
            for coord in track.findall("{http://www.google.com/kml/ext/2.2}coord"):
                vals = coord.text.split() if coord.text else []
                if len(vals) >= 2:
                    lon, lat = float(vals[0]), float(vals[1])
                    z = float(vals[2]) if len(vals) >= 3 else None
                    coords.append((lon, lat, z))
            if len(coords) >= 2:
                lines.append({"name": name, "coords": coords})
    return {"points": points, "lines": lines, "kml_text": kml_text}


def pick_first_point(uploaded_file, label="punto") -> Tuple[float, float, Optional[float], str]:
    geoms = extract_kml_geometries(uploaded_file)
    if not geoms["points"]:
        raise ValueError(f"El archivo de {label} no contiene puntos válidos.")
    p = geoms["points"][0]
    lon, lat, z = p["coords"]
    return lon, lat, z, p["name"]


def pick_longest_line(uploaded_file, label="línea") -> Tuple[List[Tuple[float, float, Optional[float]]], str]:
    geoms = extract_kml_geometries(uploaded_file)
    if not geoms["lines"]:
        raise ValueError(f"El archivo de {label} no contiene una línea válida.")
    # largo aproximado en grados solo para elegir; después se reproyecta
    def approx_len(coords):
        total = 0.0
        for a, b in zip(coords[:-1], coords[1:]):
            total += math.hypot(b[0] - a[0], b[1] - a[1])
        return total
    line = max(geoms["lines"], key=lambda d: approx_len(d["coords"]))
    return line["coords"], line["name"]


def determine_local_utm_crs(lon: float, lat: float) -> CRS:
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def transformers_for_crs(crs_proj: CRS):
    to_proj = Transformer.from_crs("EPSG:4326", crs_proj, always_xy=True)
    to_wgs = Transformer.from_crs(crs_proj, "EPSG:4326", always_xy=True)
    return to_proj, to_wgs


def coords_to_linestring_projected(coords_lonlatz, to_proj) -> Tuple[LineString, List[Optional[float]]]:
    pts = []
    zs = []
    for lon, lat, z in coords_lonlatz:
        x, y = to_proj.transform(lon, lat)
        pts.append((x, y))
        zs.append(z)
    return LineString(pts), zs


def point_projected(lon: float, lat: float, to_proj) -> Point:
    x, y = to_proj.transform(lon, lat)
    return Point(x, y)


def load_support_curves(uploaded_file, to_proj) -> List[Dict[str, Any]]:
    """Carga curvas de nivel de apoyo desde KMZ/KML o DXF.
    Retorna lista {line: LineString proyectada, elev: float|None, name: str, source:str}
    """
    if uploaded_file is None:
        return []
    name = uploaded_file.name.lower()
    curves: List[Dict[str, Any]] = []
    if name.endswith((".kml", ".kmz")):
        geoms = extract_kml_geometries(uploaded_file)
        for ln in geoms["lines"]:
            line, zs = coords_to_linestring_projected(ln["coords"], to_proj)
            elev = None
            zs_non = [z for z in zs if z is not None]
            if zs_non:
                elev = float(np.nanmedian(zs_non))
            else:
                # Intentar extraer número del nombre
                import re
                m = re.search(r"(-?\d+(?:\.\d+)?)", ln.get("name", ""))
                if m:
                    elev = float(m.group(1))
            curves.append({"line": line, "elev": elev, "name": ln.get("name", "curva"), "source": "kml/kmz"})
    elif name.endswith(".dxf"):
        if ezdxf is None:
            raise RuntimeError("ezdxf no está instalado. Agrega ezdxf al requirements.txt.")
        data = uploaded_bytes(uploaded_file)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            doc = ezdxf.readfile(tmp_path)
            msp = doc.modelspace()
            for e in msp:
                etype = e.dxftype()
                elev = None
                pts = []
                layer = getattr(e.dxf, "layer", "dxf")
                if etype == "LWPOLYLINE":
                    try:
                        elev = float(getattr(e.dxf, "elevation", 0.0))
                    except Exception:
                        elev = None
                    for p in e.get_points():
                        # p: x, y, start_width, end_width, bulge
                        pts.append((float(p[0]), float(p[1])))
                elif etype in ("POLYLINE", "3DPOLYLINE"):
                    try:
                        for v in e.vertices:
                            loc = v.dxf.location
                            pts.append((float(loc.x), float(loc.y)))
                            if elev is None:
                                elev = float(loc.z)
                    except Exception:
                        continue
                elif etype == "LINE":
                    s = e.dxf.start
                    t = e.dxf.end
                    pts = [(float(s.x), float(s.y)), (float(t.x), float(t.y))]
                    elev = float(np.nanmean([s.z, t.z])) if hasattr(s, "z") else None
                if len(pts) >= 2:
                    # Asumimos DXF en coordenadas UTM WGS84 local. No reproyectamos.
                    curves.append({"line": LineString(pts), "elev": elev, "name": layer, "source": "dxf"})
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    else:
        raise ValueError("Formato de curvas de apoyo no soportado. Use KMZ/KML/DXF.")
    return curves


def load_profile_file(uploaded_file) -> pd.DataFrame:
    if uploaded_file is None:
        return pd.DataFrame()
    name = uploaded_file.name.lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded_file)
    elif name.endswith((".csv", ".txt")):
        data = uploaded_bytes(uploaded_file)
        text = data.decode("utf-8", errors="ignore")
        # autodetectar separador básico
        sep = ";" if text.count(";") > text.count(",") else ","
        df = pd.read_csv(io.StringIO(text), sep=sep)
    elif name.endswith((".kml", ".kmz")):
        coords, _ = pick_longest_line(uploaded_file, "perfil longitudinal")
        rows = []
        cum = 0.0
        prev = None
        # Distancia aproximada en grados no sirve; se ajusta después si se usa como XY/latlon.
        for lon, lat, z in coords:
            if prev is not None:
                cum += math.hypot(lon - prev[0], lat - prev[1])
            rows.append({"lon": lon, "lat": lat, "cota": z, "distancia": cum})
            prev = (lon, lat)
        df = pd.DataFrame(rows)
    elif name.endswith(".dxf"):
        if ezdxf is None:
            raise RuntimeError("ezdxf no está instalado. Agrega ezdxf al requirements.txt.")
        data = uploaded_bytes(uploaded_file)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        rows = []
        try:
            doc = ezdxf.readfile(tmp_path)
            msp = doc.modelspace()
            longest = []
            for e in msp:
                pts = []
                if e.dxftype() == "LWPOLYLINE":
                    elev = float(getattr(e.dxf, "elevation", 0.0))
                    for p in e.get_points():
                        pts.append((float(p[0]), float(p[1]), elev))
                elif e.dxftype() in ("POLYLINE", "3DPOLYLINE"):
                    for v in e.vertices:
                        loc = v.dxf.location
                        pts.append((float(loc.x), float(loc.y), float(loc.z)))
                if len(pts) > len(longest):
                    longest = pts
            cum = 0.0
            prev = None
            for x, y, z in longest:
                if prev is not None:
                    cum += math.hypot(x - prev[0], y - prev[1])
                rows.append({"x": x, "y": y, "cota": z, "distancia": cum})
                prev = (x, y)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        df = pd.DataFrame(rows)
    else:
        raise ValueError("Formato de perfil no soportado.")
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def normalize_profile_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    # Identificación flexible
    cols = list(df.columns)
    cota_col = next((c for c in cols if c in ["cota", "z", "elev", "elevacion", "elevación", "altitud"]), None)
    dist_col = next((c for c in cols if c in ["km", "dist", "distancia", "distancia_m", "chainage", "station"]), None)
    if cota_col is None:
        raise ValueError("El perfil debe contener una columna de cota: cota, z, elev, elevacion o altitud.")
    out = pd.DataFrame()
    if dist_col is not None:
        dist = pd.to_numeric(df[dist_col], errors="coerce")
        # Si parece estar en km, convertir a m para interpolación interna
        if dist.max(skipna=True) is not None and dist.max(skipna=True) < 1000 and "km" in dist_col:
            dist_m = dist * 1000.0
        else:
            dist_m = dist
        out["dist_m"] = dist_m
    elif "x" in cols and "y" in cols:
        x = pd.to_numeric(df["x"], errors="coerce")
        y = pd.to_numeric(df["y"], errors="coerce")
        dist_m = [0.0]
        for i in range(1, len(df)):
            dist_m.append(dist_m[-1] + float(math.hypot(x.iloc[i] - x.iloc[i-1], y.iloc[i] - y.iloc[i-1])))
        out["dist_m"] = dist_m
    else:
        # fallback índice
        out["dist_m"] = np.arange(len(df), dtype=float)
    out["cota"] = pd.to_numeric(df[cota_col], errors="coerce")
    out = out.dropna().sort_values("dist_m")
    out = out.drop_duplicates("dist_m")
    return out




# --------------------------------------------------------------------------------------
# Descarga DEM desde OpenTopography
# --------------------------------------------------------------------------------------
class BytesUpload:
    """Objeto mínimo compatible con uploaded_bytes para usar DEM descargado por API."""
    def __init__(self, name: str, data: bytes):
        self.name = name
        self._bio = io.BytesIO(data)

    def seek(self, pos: int):
        return self._bio.seek(pos)

    def read(self, *args, **kwargs):
        return self._bio.read(*args, **kwargs)


def bbox_from_lonlat_inputs(axis_coords, pc_h_lon, pc_h_lat, pc_s_lon, pc_s_lat, margin_km: float):
    """Calcula bbox WGS84 para OpenTopography a partir del eje y PCs, con margen en km."""
    lons = [float(c[0]) for c in axis_coords] + [float(pc_h_lon), float(pc_s_lon)]
    lats = [float(c[1]) for c in axis_coords] + [float(pc_h_lat), float(pc_s_lat)]
    lat_mid = float(np.nanmean(lats)) if lats else 0.0
    deg_lat = float(margin_km) / 111.32
    cos_lat = max(0.15, abs(math.cos(math.radians(lat_mid))))
    deg_lon = float(margin_km) / (111.32 * cos_lat)
    south = max(-90.0, min(lats) - deg_lat)
    north = min(90.0, max(lats) + deg_lat)
    west = max(-180.0, min(lons) - deg_lon)
    east = min(180.0, max(lons) + deg_lon)
    return west, south, east, north


def estimate_bbox_area_km2(west: float, south: float, east: float, north: float) -> float:
    lat_mid = (south + north) / 2.0
    width = max(0.0, east - west) * 111.32 * max(0.15, abs(math.cos(math.radians(lat_mid))))
    height = max(0.0, north - south) * 111.32
    return width * height


def download_opentopography_dem(api_key: str, demtype: str, west: float, south: float, east: float, north: float) -> bytes:
    """Descarga DEM global de OpenTopography como GeoTIFF.

    No imprime ni guarda la API Key. Lanza excepción con mensaje controlado si falla.
    """
    if not api_key or not str(api_key).strip():
        raise ValueError("Debe ingresar una API Key de OpenTopography.")
    if not (south < north and west < east):
        raise ValueError("Bounding box inválido: verifique west/east/south/north.")
    url = "https://portal.opentopography.org/API/globaldem"
    params = {
        "demtype": demtype,
        "south": f"{south:.8f}",
        "north": f"{north:.8f}",
        "west": f"{west:.8f}",
        "east": f"{east:.8f}",
        "outputFormat": "GTiff",
        "API_Key": api_key.strip(),
    }
    try:
        r = requests.get(url, params=params, timeout=180)
    except requests.RequestException as exc:
        raise RuntimeError(f"No se pudo conectar con OpenTopography: {exc}") from exc
    if r.status_code != 200:
        txt = r.text[:600] if hasattr(r, 'text') else ''
        raise RuntimeError(f"OpenTopography respondió código {r.status_code}. Revise API Key, DEM, bbox o límites del servicio. Respuesta: {txt}")
    data = r.content
    if len(data) < 1024:
        txt = data.decode('utf-8', errors='ignore')[:600]
        raise RuntimeError(f"La respuesta de OpenTopography no parece ser un GeoTIFF válido: {txt}")
    head = data[:20].lower()
    if head.startswith(b'{') or head.startswith(b'<') or b'error' in head:
        txt = data.decode('utf-8', errors='ignore')[:600]
        raise RuntimeError(f"OpenTopography no entregó un GeoTIFF válido: {txt}")
    return data


# --------------------------------------------------------------------------------------
# Geometría del eje y tramo útil
# --------------------------------------------------------------------------------------
def orient_axis_by_dem_or_user(axis: LineString, dem_sampler, user_invert: bool = False) -> Tuple[LineString, bool, str, Optional[float], Optional[float]]:
    coords = list(axis.coords)
    z0 = z1 = None
    inverted = False
    direction_note = "Sentido conservado según digitalización original."
    if dem_sampler is not None:
        z0 = dem_sampler(coords[0][0], coords[0][1])
        z1 = dem_sampler(coords[-1][0], coords[-1][1])
        if z0 is not None and z1 is not None and np.isfinite(z0) and np.isfinite(z1):
            # Aguas arriba mayor cota. Línea se orienta aguas arriba -> aguas abajo.
            if z0 < z1:
                coords = list(reversed(coords))
                inverted = True
                direction_note = "Eje invertido automáticamente: extremo final tenía mayor cota DEM."
            else:
                direction_note = "Eje conservado: extremo inicial tiene mayor cota DEM."
        else:
            direction_note = "No se pudo determinar sentido por DEM; se conserva digitalización."
    if user_invert:
        coords = list(reversed(coords))
        inverted = not inverted
        direction_note += " Inversión manual aplicada por usuario."
    return LineString(coords), inverted, direction_note, z0, z1


def extract_subline(line: LineString, d0: float, d1: float) -> LineString:
    """Extrae sublínea entre distancias d0 y d1 sobre una LineString."""
    if d1 < d0:
        d0, d1 = d1, d0
    d0 = max(0.0, min(float(d0), line.length))
    d1 = max(0.0, min(float(d1), line.length))
    if d1 <= d0:
        raise ValueError("El tramo útil tiene longitud cero. Revise PCs y eje.")
    coords = list(line.coords)
    new_pts = [line.interpolate(d0).coords[0]]
    dist_acc = 0.0
    for a, b in zip(coords[:-1], coords[1:]):
        seg_len = math.hypot(b[0] - a[0], b[1] - a[1])
        next_acc = dist_acc + seg_len
        if next_acc > d0 and dist_acc < d1:
            if dist_acc >= d0 and next_acc <= d1:
                new_pts.append(b)
        dist_acc = next_acc
    new_pts.append(line.interpolate(d1).coords[0])
    # eliminar duplicados consecutivos
    cleaned = []
    for p in new_pts:
        if not cleaned or math.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 1e-6:
            cleaned.append(p)
    if len(cleaned) < 2:
        raise ValueError("No se pudo extraer tramo útil válido.")
    return LineString(cleaned)


def tangent_at_distance(line: LineString, d: float, eps: float = 1.0) -> Tuple[float, float]:
    d0 = max(0.0, d - eps)
    d1 = min(line.length, d + eps)
    if d1 <= d0:
        d0 = max(0.0, d - 5.0)
        d1 = min(line.length, d + 5.0)
    p0 = line.interpolate(d0)
    p1 = line.interpolate(d1)
    dx = p1.x - p0.x
    dy = p1.y - p0.y
    n = math.hypot(dx, dy)
    if n == 0:
        return (1.0, 0.0)
    return (dx / n, dy / n)


def cross_section_line(line: LineString, d: float, half_width: float) -> LineString:
    p = line.interpolate(d)
    tx, ty = tangent_at_distance(line, d)
    # Normal izquierda mirando aguas abajo
    nx, ny = -ty, tx
    left = (p.x + nx * half_width, p.y + ny * half_width)
    right = (p.x - nx * half_width, p.y - ny * half_width)
    # HEC-RAS: puntos left-to-right looking downstream
    return LineString([left, right])


def make_station_points(section: LineString, station_step: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    length = section.length
    n = max(2, int(math.ceil(length / station_step)) + 1)
    ds = np.linspace(0.0, length, n)
    xs = np.array([section.interpolate(float(d)).x for d in ds])
    ys = np.array([section.interpolate(float(d)).y for d in ds])
    return ds, xs, ys


# --------------------------------------------------------------------------------------
# DEM sampler
# --------------------------------------------------------------------------------------
class DemSampler:
    def __init__(self, uploaded_file=None):
        self.dataset = None
        self.path = None
        self.crs = None
        self.transformer_from_proj = None
        self.nodata = None
        if uploaded_file is not None:
            if rasterio is None:
                raise RuntimeError("rasterio no está instalado.")
            data = uploaded_bytes(uploaded_file)
            suffix = ".tif" if uploaded_file.name.lower().endswith(".tif") else ".tiff"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.write(data)
            tmp.close()
            self.path = tmp.name
            self.dataset = rasterio.open(self.path)
            self.crs = self.dataset.crs
            self.nodata = self.dataset.nodata

    def close(self):
        try:
            if self.dataset:
                self.dataset.close()
        finally:
            if self.path and os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except Exception:
                    pass

    def sample_xy(self, x: float, y: float, source_crs: CRS) -> Optional[float]:
        if self.dataset is None:
            return None
        if self.crs is None:
            return None
        if CRS.from_user_input(source_crs) != CRS.from_user_input(self.crs):
            transformer = Transformer.from_crs(source_crs, self.crs, always_xy=True)
            xx, yy = transformer.transform(x, y)
        else:
            xx, yy = x, y
        try:
            val = next(self.dataset.sample([(xx, yy)]))[0]
            if self.nodata is not None and float(val) == float(self.nodata):
                return None
            if not np.isfinite(val):
                return None
            return float(val)
        except Exception:
            return None

    def sample_many_xy(self, xs: Sequence[float], ys: Sequence[float], source_crs: CRS) -> np.ndarray:
        if self.dataset is None:
            return np.full(len(xs), np.nan)
        if CRS.from_user_input(source_crs) != CRS.from_user_input(self.crs):
            transformer = Transformer.from_crs(source_crs, self.crs, always_xy=True)
            pts = [transformer.transform(float(x), float(y)) for x, y in zip(xs, ys)]
        else:
            pts = list(zip(xs, ys))
        vals = []
        try:
            for v in self.dataset.sample(pts):
                val = float(v[0])
                if self.nodata is not None and val == float(self.nodata):
                    vals.append(np.nan)
                elif np.isfinite(val):
                    vals.append(val)
                else:
                    vals.append(np.nan)
        except Exception:
            vals = [np.nan] * len(xs)
        return np.array(vals, dtype=float)


# --------------------------------------------------------------------------------------
# Perfil longitudinal y secciones
# --------------------------------------------------------------------------------------
def generate_longitudinal_profile(line: LineString, dem: Optional[DemSampler], crs_proj: CRS, step_m: float = 25.0, respaldo: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    n = max(2, int(math.ceil(line.length / step_m)) + 1)
    dists = np.linspace(0.0, line.length, n)
    xs = np.array([line.interpolate(float(d)).x for d in dists])
    ys = np.array([line.interpolate(float(d)).y for d in dists])
    if dem is not None and dem.dataset is not None:
        z_dem = dem.sample_many_xy(xs, ys, crs_proj)
    else:
        z_dem = np.full(n, np.nan)
    z_res = np.full(n, np.nan)
    if respaldo is not None and not respaldo.empty:
        r = normalize_profile_df(respaldo)
        if not r.empty and r["dist_m"].max() > r["dist_m"].min():
            # Escala perfil respaldo al largo del tramo útil si difiere razonablemente.
            rr_dist = r["dist_m"].to_numpy(dtype=float)
            rr_z = r["cota"].to_numpy(dtype=float)
            scale = line.length / rr_dist.max() if rr_dist.max() > 0 else 1.0
            rr_scaled = rr_dist * scale
            z_res = np.interp(dists, rr_scaled, rr_z, left=np.nan, right=np.nan)
    diff = z_dem - z_res
    slope = np.full(n, np.nan)
    if np.isfinite(z_dem).sum() >= 2:
        for i in range(1, n):
            if np.isfinite(z_dem[i]) and np.isfinite(z_dem[i-1]) and dists[i] > dists[i-1]:
                slope[i] = (z_dem[i-1] - z_dem[i]) / (dists[i] - dists[i-1])
    return pd.DataFrame({
        "km": dists / 1000.0,
        "dist_m": dists,
        "x": xs,
        "y": ys,
        "cota_dem": z_dem,
        "cota_respaldo": z_res,
        "diferencia_dem_menos_respaldo": diff,
        "pendiente_local_m_m": slope,
    })


def evaluate_profile(profile_df: pd.DataFrame) -> List[str]:
    warnings = []
    if profile_df.empty:
        return ["No se generó perfil longitudinal."]
    z = profile_df["cota_dem"].to_numpy(dtype=float)
    d = profile_df["dist_m"].to_numpy(dtype=float)
    finite = np.isfinite(z)
    if finite.sum() < max(3, len(z) // 4):
        warnings.append("El perfil DEM tiene muchas cotas sin dato. Revise cobertura del DEM.")
    if finite.sum() >= 2:
        # Eje orientado aguas arriba -> aguas abajo: debería tender a descender.
        dz_total = z[finite][0] - z[finite][-1]
        if dz_total < 0:
            warnings.append("El perfil DEM aumenta aguas abajo. Revise sentido del eje o calidad del DEM.")
        # Saltos locales grandes relativos
        dz = np.diff(z)
        dd = np.diff(d)
        with np.errstate(invalid="ignore", divide="ignore"):
            grad = np.abs(dz / dd)
        if np.nanmax(grad) > 0.50:
            warnings.append("Se detectan saltos de pendiente local muy altos en el perfil DEM (>50%). Revise eje/DEM/topografía.")
    if "cota_respaldo" in profile_df.columns and np.isfinite(profile_df["cota_respaldo"]).sum() > 2:
        diff = profile_df["diferencia_dem_menos_respaldo"].to_numpy(dtype=float)
        mad = np.nanmean(np.abs(diff))
        mx = np.nanmax(np.abs(diff))
        if mad > 5:
            warnings.append(f"Diferencia media DEM vs perfil respaldo alta: {mad:.2f} m.")
        if mx > 15:
            warnings.append(f"Diferencia máxima DEM vs perfil respaldo alta: {mx:.2f} m.")
    return warnings


def generate_natural_sections(line: LineString, dem: DemSampler, crs_proj: CRS, spacing_m: float, half_width_m: float, station_step_m: float) -> List[CrossSection]:
    if dem is None or dem.dataset is None:
        return []
    dists = np.arange(0.0, line.length + 0.001, spacing_m)
    if dists[-1] < line.length:
        dists = np.append(dists, line.length)
    sections: List[CrossSection] = []
    for d in dists:
        sec_line = cross_section_line(line, float(d), half_width_m)
        st, xs, ys = make_station_points(sec_line, station_step_m)
        zs = dem.sample_many_xy(xs, ys, crs_proj)
        status = "aceptada" if np.isfinite(zs).sum() >= max(3, len(zs) // 2) else "revisar"
        notes = "Generada desde DEM."
        if status == "revisar":
            notes += " Muchas cotas sin dato."
        center = line.interpolate(float(d))
        sections.append(CrossSection(
            km=float(d) / 1000.0,
            center_x=center.x,
            center_y=center.y,
            left_x=sec_line.coords[0][0],
            left_y=sec_line.coords[0][1],
            right_x=sec_line.coords[-1][0],
            right_y=sec_line.coords[-1][1],
            stations=st.tolist(),
            elevations=[float(v) if np.isfinite(v) else np.nan for v in zs],
            source="DEM",
            status=status,
            notes=notes,
        ))
    return sections


def generate_prismatic_sections(line: LineString, profile_df: pd.DataFrame, spacing_m: float, kind: str, bottom_width: float, height: float, side_slope_l: float, side_slope_r: float, manning_n: float) -> List[CrossSection]:
    dists = np.arange(0.0, line.length + 0.001, spacing_m)
    if dists[-1] < line.length:
        dists = np.append(dists, line.length)
    # interp cota fondo desde perfil DEM si existe, si no 0
    if profile_df is not None and not profile_df.empty and np.isfinite(profile_df.get("cota_dem", pd.Series(dtype=float))).sum() > 1:
        ref_d = profile_df["dist_m"].to_numpy(dtype=float)
        ref_z = profile_df["cota_dem"].to_numpy(dtype=float)
        mask = np.isfinite(ref_z)
        z_beds = np.interp(dists, ref_d[mask], ref_z[mask], left=ref_z[mask][0], right=ref_z[mask][-1])
    else:
        z_beds = np.zeros(len(dists))
    sections: List[CrossSection] = []
    for d, zbed in zip(dists, z_beds):
        if kind.lower().startswith("rect"):
            stations = [0.0, bottom_width, bottom_width]
            elevations = [zbed + height, zbed + height, zbed]
            # Mejor como 4 puntos left wall, bottom L, bottom R, right wall
            stations = [0.0, 0.0, bottom_width, bottom_width]
            elevations = [zbed + height, zbed, zbed, zbed + height]
            top_width = bottom_width
        else:
            wl = side_slope_l * height
            wr = side_slope_r * height
            top_width = wl + bottom_width + wr
            stations = [0.0, wl, wl + bottom_width, top_width]
            elevations = [zbed + height, zbed, zbed, zbed + height]
        sec_line = cross_section_line(line, float(d), max(top_width / 2, bottom_width / 2, 1.0))
        center = line.interpolate(float(d))
        sections.append(CrossSection(
            km=float(d) / 1000.0,
            center_x=center.x,
            center_y=center.y,
            left_x=sec_line.coords[0][0],
            left_y=sec_line.coords[0][1],
            right_x=sec_line.coords[-1][0],
            right_y=sec_line.coords[-1][1],
            stations=[float(s) for s in stations],
            elevations=[float(e) for e in elevations],
            source=f"{kind} n={manning_n}",
            status="aceptada",
            notes="Sección prismática generada por parámetros.",
        ))
    return sections


def load_prismatic_sections_excel(uploaded_file, line: LineString, profile_df: pd.DataFrame) -> List[CrossSection]:
    if uploaded_file is None:
        return []
    df = pd.read_excel(uploaded_file)
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = ["km", "tipo_seccion", "ancho_fondo_m", "altura_m"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"El Excel de secciones no contiene columnas: {', '.join(missing)}")
    sections = []
    ref_d = ref_z = None
    if profile_df is not None and not profile_df.empty and np.isfinite(profile_df.get("cota_dem", pd.Series(dtype=float))).sum() > 1:
        ref_d = profile_df["dist_m"].to_numpy(dtype=float)
        ref_z = profile_df["cota_dem"].to_numpy(dtype=float)
        mask = np.isfinite(ref_z)
        ref_d, ref_z = ref_d[mask], ref_z[mask]
    for _, row in df.iterrows():
        km = float(row["km"])
        d = km * 1000.0
        if d < 0 or d > line.length:
            continue
        kind = str(row["tipo_seccion"]).lower()
        b = float(row["ancho_fondo_m"])
        h = float(row["altura_m"])
        zl = float(row.get("talud_izq_hv", 0.0) or 0.0)
        zr = float(row.get("talud_der_hv", 0.0) or 0.0)
        zbed = row.get("cota_fondo", np.nan)
        try:
            zbed = float(zbed)
        except Exception:
            zbed = np.nan
        if not np.isfinite(zbed):
            if ref_d is not None and len(ref_d) > 1:
                zbed = float(np.interp(d, ref_d, ref_z))
            else:
                zbed = 0.0
        n = float(row.get("manning_n", 0.035) or 0.035)
        if kind.startswith("rect"):
            top_width = b
            stations = [0.0, 0.0, b, b]
            elevations = [zbed + h, zbed, zbed, zbed + h]
        else:
            wl = zl * h
            wr = zr * h
            top_width = wl + b + wr
            stations = [0.0, wl, wl + b, top_width]
            elevations = [zbed + h, zbed, zbed, zbed + h]
        sec_line = cross_section_line(line, d, max(top_width / 2, 1.0))
        center = line.interpolate(d)
        sections.append(CrossSection(
            km=km,
            center_x=center.x,
            center_y=center.y,
            left_x=sec_line.coords[0][0],
            left_y=sec_line.coords[0][1],
            right_x=sec_line.coords[-1][0],
            right_y=sec_line.coords[-1][1],
            stations=[float(s) for s in stations],
            elevations=[float(e) for e in elevations],
            source=f"excel_{kind} n={n}",
            status="aceptada",
            notes=str(row.get("observacion", "")),
        ))
    return sections


def evaluate_curve_support(line: LineString, sections: List[CrossSection], curves: List[Dict[str, Any]], corridor_m: float = 1000.0) -> Dict[str, Any]:
    if not curves:
        return {"estado": "sin_curvas", "detalle": "No se cargaron curvas de apoyo.", "secciones_cubiertas_pct": 0.0}
    covered = 0
    checked = 0
    for sec in sections:
        sec_line = LineString([(sec.left_x, sec.left_y), (sec.right_x, sec.right_y)])
        center = Point(sec.center_x, sec.center_y)
        left_hits = 0
        right_hits = 0
        tx, ty = tangent_at_distance(line, sec.km * 1000.0)
        nx, ny = -ty, tx
        for c in curves:
            # Filtro rápido por bbox antes de calcular intersección
            sb = sec_line.bounds
            cb = c["line"].bounds
            if sb[2] < cb[0] or sb[0] > cb[2] or sb[3] < cb[1] or sb[1] > cb[3]:
                continue
            inter = sec_line.intersection(c["line"])
            if inter.is_empty:
                continue
            pts = []
            if inter.geom_type == "Point":
                pts = [inter]
            elif inter.geom_type == "MultiPoint":
                pts = list(inter.geoms)
            elif inter.geom_type in ("LineString", "MultiLineString"):
                pts = [inter.interpolate(0.5, normalized=True)]
            for p in pts:
                side = (p.x - center.x) * nx + (p.y - center.y) * ny
                if side >= 0:
                    left_hits += 1
                else:
                    right_hits += 1
        checked += 1
        if left_hits >= 1 and right_hits >= 1:
            covered += 1
    pct = 100.0 * covered / checked if checked else 0.0
    if pct >= 80:
        estado = "suficiente"
    elif pct >= 40:
        estado = "parcial"
    else:
        estado = "insuficiente"
    return {"estado": estado, "detalle": f"{covered}/{checked} secciones con curvas a ambos lados del eje.", "secciones_cubiertas_pct": pct}


# --------------------------------------------------------------------------------------
# Exportaciones
# --------------------------------------------------------------------------------------
def _kml_coord(lon: float, lat: float, z: Optional[float] = None) -> str:
    if z is None or not np.isfinite(z):
        return f"{lon:.8f},{lat:.8f}"
    return f"{lon:.8f},{lat:.8f},{float(z):.3f}"


def kml_linestring(name: str, coords_lonlat: List[Tuple[float, float, Optional[float]]], style_url: str = "") -> str:
    coord_text = " ".join([_kml_coord(lon, lat, z) for lon, lat, z in coords_lonlat])
    alt_mode = "<altitudeMode>absolute</altitudeMode>" if any(z is not None for _, _, z in coords_lonlat) else ""
    return f"""
    <Placemark><name>{escape(name)}</name>{style_url}<LineString><tessellate>1</tessellate>{alt_mode}<coordinates>{coord_text}</coordinates></LineString></Placemark>"""


def kml_point(name: str, lon: float, lat: float, z: Optional[float] = None, style_url: str = "") -> str:
    coord = _kml_coord(lon, lat, z)
    return f"""
    <Placemark><name>{escape(name)}</name>{style_url}<Point><coordinates>{coord}</coordinates></Point></Placemark>"""


def line_to_lonlatz(line: LineString, to_wgs, z_values: Optional[Sequence[float]] = None) -> List[Tuple[float, float, Optional[float]]]:
    coords = list(line.coords)
    out = []
    for i, (x, y) in enumerate(coords):
        lon, lat = to_wgs.transform(x, y)
        z = None
        if z_values is not None and i < len(z_values):
            z = float(z_values[i]) if np.isfinite(z_values[i]) else None
        out.append((lon, lat, z))
    return out


def make_kmz(axis_full: LineString, axis_useful: LineString, pc_h: Point, pc_s: Point, sections: List[CrossSection], to_wgs, out_name="hidrosed_eje_secciones.kmz") -> bytes:
    styles = """
    <Style id="axis_full"><LineStyle><color>ff777777</color><width>2</width></LineStyle></Style>
    <Style id="axis_useful"><LineStyle><color>ffff0000</color><width>4</width></LineStyle></Style>
    <Style id="pc_h"><IconStyle><color>ff0000ff</color><scale>1.2</scale></IconStyle></Style>
    <Style id="pc_s"><IconStyle><color>ff00ff00</color><scale>1.2</scale></IconStyle></Style>
    <Style id="section_ok"><LineStyle><color>ff00ffff</color><width>2</width></LineStyle></Style>
    <Style id="section_review"><LineStyle><color>ff00a5ff</color><width>2</width></LineStyle></Style>
    """
    placemarks = []
    placemarks.append(kml_linestring("Eje completo", line_to_lonlatz(axis_full, to_wgs), "<styleUrl>#axis_full</styleUrl>"))
    placemarks.append(kml_linestring("Eje útil PC hidrológico - PC cuenca soporte", line_to_lonlatz(axis_useful, to_wgs), "<styleUrl>#axis_useful</styleUrl>"))
    lon, lat = to_wgs.transform(pc_h.x, pc_h.y)
    placemarks.append(kml_point("PC hidrológico proyectado", lon, lat, None, "<styleUrl>#pc_h</styleUrl>"))
    lon, lat = to_wgs.transform(pc_s.x, pc_s.y)
    placemarks.append(kml_point("PC cuenca soporte proyectado", lon, lat, None, "<styleUrl>#pc_s</styleUrl>"))
    for i, sec in enumerate(sections, start=1):
        line = LineString([(sec.left_x, sec.left_y), (sec.right_x, sec.right_y)])
        style = "#section_ok" if sec.status == "aceptada" else "#section_review"
        placemarks.append(kml_linestring(f"XS {i:03d} km {sec.km:.3f} {sec.status}", line_to_lonlatz(line, to_wgs), f"<styleUrl>{style}</styleUrl>"))
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>HidroSed Eje Cauce y Secciones</name>{styles}{''.join(placemarks)}</Document></kml>"""
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
    mem.seek(0)
    return mem.read()


def sections_to_hecras_excel(sections: List[CrossSection]) -> bytes:
    mem = io.BytesIO()
    rows = []
    for sec in sections:
        for st, elev in zip(sec.stations, sec.elevations):
            rows.append({
                "River Station_km": sec.km,
                "Station_m": st,
                "Elevation_m": elev,
                "Source": sec.source,
                "Status": sec.status,
                "Notes": sec.notes,
            })
    df_points = pd.DataFrame(rows)
    df_summary = pd.DataFrame([{
        "km": s.km,
        "center_x": s.center_x,
        "center_y": s.center_y,
        "left_x": s.left_x,
        "left_y": s.left_y,
        "right_x": s.right_x,
        "right_y": s.right_y,
        "source": s.source,
        "status": s.status,
        "notes": s.notes,
    } for s in sections])
    with pd.ExcelWriter(mem, engine="openpyxl") as writer:
        df_summary.to_excel(writer, index=False, sheet_name="secciones_resumen")
        df_points.to_excel(writer, index=False, sheet_name="hecras_station_elevation")
    mem.seek(0)
    return mem.read()


def make_prismatic_template() -> bytes:
    df = pd.DataFrame({
        "km": [0.0, 0.1, 0.2],
        "tipo_seccion": ["trapecial", "trapecial", "rectangular"],
        "ancho_fondo_m": [4.0, 4.5, 3.0],
        "altura_m": [2.0, 2.0, 1.5],
        "talud_izq_HV": [1.5, 1.5, 0.0],
        "talud_der_HV": [1.5, 1.5, 0.0],
        "manning_n": [0.035, 0.035, 0.030],
        "cota_fondo": [np.nan, np.nan, np.nan],
        "observacion": ["ejemplo", "ejemplo", "ejemplo"],
    })
    mem = io.BytesIO()
    with pd.ExcelWriter(mem, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="secciones")
    mem.seek(0)
    return mem.read()


# --------------------------------------------------------------------------------------
# Visualización
# --------------------------------------------------------------------------------------
def plot_axis_sections(axis_full, axis_useful, pc_h, pc_s, sections, curves=None):
    fig, ax = plt.subplots(figsize=(9, 7))
    xf, yf = axis_full.xy
    ax.plot(xf, yf, linewidth=1.5, label="Eje completo")
    xu, yu = axis_useful.xy
    ax.plot(xu, yu, linewidth=3, label="Eje útil")
    ax.scatter([pc_h.x], [pc_h.y], s=60, marker="o", label="PC hidrológico proyectado")
    ax.scatter([pc_s.x], [pc_s.y], s=60, marker="s", label="PC soporte proyectado")
    if curves:
        for c in curves[:200]:
            try:
                x, y = c["line"].xy
                ax.plot(x, y, linewidth=0.5, alpha=0.35)
            except Exception:
                pass
    for sec in sections[:300]:
        ax.plot([sec.left_x, sec.right_x], [sec.left_y, sec.right_y], linewidth=0.8, alpha=0.75)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    ax.set_title("Eje útil y secciones transversales")
    ax.set_xlabel("X UTM [m]")
    ax.set_ylabel("Y UTM [m]")
    return fig


def plot_profile(profile_df):
    fig, ax = plt.subplots(figsize=(10, 4))
    if "cota_dem" in profile_df:
        ax.plot(profile_df["km"], profile_df["cota_dem"], label="DEM")
    if "cota_respaldo" in profile_df and np.isfinite(profile_df["cota_respaldo"]).sum() > 1:
        ax.plot(profile_df["km"], profile_df["cota_respaldo"], label="Perfil respaldo")
    ax.set_xlabel("km")
    ax.set_ylabel("Cota [m]")
    ax.set_title("Perfil longitudinal del tramo útil")
    ax.grid(True, alpha=0.3)
    ax.legend()
    return fig


def plot_sections_3d(axis_useful, sections):
    if go is None:
        return None
    fig = go.Figure()
    x, y = axis_useful.xy
    fig.add_trace(go.Scatter3d(x=list(x), y=list(y), z=[0] * len(x), mode="lines", name="Eje útil XY"))
    for i, sec in enumerate(sections[:80]):
        xs = np.linspace(sec.left_x, sec.right_x, len(sec.elevations))
        ys = np.linspace(sec.left_y, sec.right_y, len(sec.elevations))
        z = np.array(sec.elevations, dtype=float)
        fig.add_trace(go.Scatter3d(x=xs, y=ys, z=z, mode="lines", name=f"XS {sec.km:.3f} km", showlegend=(i < 5)))
    fig.update_layout(scene=dict(xaxis_title="X", yaxis_title="Y", zaxis_title="Cota"), height=650, title="Vista 3D referencial eje + secciones")
    return fig


# --------------------------------------------------------------------------------------
# Visualización avanzada, edición y estado de la aplicación
# --------------------------------------------------------------------------------------
def section_xyz(sec: CrossSection) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Retorna station, x, y, z de una sección, de izquierda a derecha mirando aguas abajo."""
    st = np.array(sec.stations, dtype=float)
    z = np.array(sec.elevations, dtype=float)
    if len(st) == 0:
        return st, np.array([]), np.array([]), z
    total = max(float(st[-1] - st[0]), 1e-9)
    frac = (st - st[0]) / total
    xs = sec.left_x + frac * (sec.right_x - sec.left_x)
    ys = sec.left_y + frac * (sec.right_y - sec.left_y)
    return st, xs, ys, z


def section_to_edit_df(sec: CrossSection) -> pd.DataFrame:
    return pd.DataFrame({
        "station_m": [float(v) for v in sec.stations],
        "cota_m": [float(v) if np.isfinite(v) else np.nan for v in sec.elevations],
    })


def apply_section_edit(sec: CrossSection, edit_df: pd.DataFrame, status: str, notes: str) -> CrossSection:
    df = edit_df.copy()
    df["station_m"] = pd.to_numeric(df["station_m"], errors="coerce")
    df["cota_m"] = pd.to_numeric(df["cota_m"], errors="coerce")
    df = df.dropna(subset=["station_m", "cota_m"]).sort_values("station_m", kind="mergesort")
    if len(df) < 2:
        raise ValueError("La sección debe mantener al menos dos puntos station-cota.")
    sec.stations = [float(v) for v in df["station_m"].to_list()]
    sec.elevations = [float(v) for v in df["cota_m"].to_list()]
    sec.status = status
    sec.notes = notes
    return sec


def smooth_section(sec: CrossSection, window: int = 3) -> CrossSection:
    z = pd.Series(sec.elevations, dtype="float64")
    if len(z) >= 3:
        zz = z.rolling(window=max(3, int(window)), center=True, min_periods=1).mean().to_numpy(dtype=float)
        # Mantener extremos para no cambiar artificialmente márgenes
        zz[0] = float(sec.elevations[0])
        zz[-1] = float(sec.elevations[-1])
        sec.elevations = [float(v) for v in zz]
        sec.notes = (sec.notes or "") + " | suavizada"
    return sec


def interpolate_section_from_neighbors(sections: List[CrossSection], idx: int) -> Optional[CrossSection]:
    if idx <= 0 or idx >= len(sections) - 1:
        return None
    prev_s = sections[idx - 1]
    next_s = sections[idx + 1]
    sec = sections[idx]
    st = np.array(sec.stations, dtype=float)
    if len(st) < 2:
        return None
    zp = np.interp(st, np.array(prev_s.stations, dtype=float), np.array(prev_s.elevations, dtype=float))
    zn = np.interp(st, np.array(next_s.stations, dtype=float), np.array(next_s.elevations, dtype=float))
    sec.elevations = [float(v) for v in (zp + zn) / 2.0]
    sec.status = "corregida"
    sec.notes = (sec.notes or "") + " | interpolada entre secciones vecinas"
    return sec


def plot_single_section(sec: CrossSection):
    fig, ax = plt.subplots(figsize=(9, 4.5))
    stn = np.array(sec.stations, dtype=float)
    elev = np.array(sec.elevations, dtype=float)
    ax.plot(stn, elev, marker="o", linewidth=1.6)
    ax.fill_between(stn, elev, np.nanmin(elev) - 1.0 if np.isfinite(elev).any() else 0, alpha=0.15)
    ax.set_xlabel("Station [m] · izquierda → derecha mirando aguas abajo")
    ax.set_ylabel("Cota [m]")
    ax.set_title(f"Sección independiente · km {sec.km:.3f} · {sec.status}")
    ax.grid(True, alpha=0.3)
    return fig


def plot_profile_interactive(profile_df: pd.DataFrame, sections: List[CrossSection]):
    if go is None:
        return None
    fig = go.Figure()
    if profile_df is not None and not profile_df.empty:
        if "cota_dem" in profile_df.columns:
            fig.add_trace(go.Scatter(
                x=profile_df["km"], y=profile_df["cota_dem"], mode="lines", name="Perfil DEM",
                hovertemplate="km=%{x:.3f}<br>cota DEM=%{y:.2f} m<extra></extra>"
            ))
        if "cota_respaldo" in profile_df.columns and np.isfinite(profile_df["cota_respaldo"]).sum() > 1:
            fig.add_trace(go.Scatter(
                x=profile_df["km"], y=profile_df["cota_respaldo"], mode="lines", name="Perfil respaldo",
                hovertemplate="km=%{x:.3f}<br>cota respaldo=%{y:.2f} m<extra></extra>"
            ))
    if sections:
        xs = [s.km for s in sections]
        ys = []
        colors = []
        labels = []
        for s in sections:
            z = np.array(s.elevations, dtype=float)
            ys.append(float(np.nanmin(z)) if np.isfinite(z).any() else np.nan)
            colors.append(s.status)
            labels.append(f"km {s.km:.3f}<br>estado: {s.status}<br>{escape(s.notes or '')}")
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="markers", name="Secciones",
            marker=dict(size=7), text=labels, hovertemplate="%{text}<br>cota fondo/ref=%{y:.2f} m<extra></extra>"
        ))
    fig.update_layout(
        title="Perfil longitudinal interactivo con ubicación de secciones",
        xaxis_title="km tramo útil",
        yaxis_title="Cota [m]",
        height=450,
        hovermode="closest",
    )
    return fig


def plot_sections_3d_interactive(axis_useful: LineString, profile_df: pd.DataFrame, sections: List[CrossSection]):
    if go is None:
        return None
    fig = go.Figure()
    # Eje 3D con cota DEM/respaldo si existe
    coords = list(axis_useful.coords)
    if profile_df is not None and not profile_df.empty and "cota_dem" in profile_df.columns and np.isfinite(profile_df["cota_dem"]).sum() > 1:
        d_prof = profile_df["dist_m"].to_numpy(dtype=float)
        z_prof = profile_df["cota_dem"].to_numpy(dtype=float)
        mask = np.isfinite(z_prof)
        d_axis = np.linspace(0, axis_useful.length, len(coords))
        z_axis = np.interp(d_axis, d_prof[mask], z_prof[mask])
    else:
        z_axis = np.zeros(len(coords), dtype=float)
    fig.add_trace(go.Scatter3d(
        x=[p[0] for p in coords], y=[p[1] for p in coords], z=z_axis,
        mode="lines", name="Eje útil 3D", line=dict(width=5)
    ))
    # Secciones
    for i, sec in enumerate(sections[:200]):
        stn, xs, ys, z = section_xyz(sec)
        if len(xs) == 0:
            continue
        fig.add_trace(go.Scatter3d(
            x=xs, y=ys, z=z, mode="lines+markers", name=f"XS km {sec.km:.3f}",
            showlegend=(i < 8),
            hovertemplate=f"XS km {sec.km:.3f}<br>station=%{{text:.2f}} m<br>cota=%{{z:.2f}} m<extra></extra>",
            text=stn,
        ))
    fig.update_layout(
        title="Perfil longitudinal 3D interactivo con secciones transversales",
        scene=dict(xaxis_title="X UTM [m]", yaxis_title="Y UTM [m]", zaxis_title="Cota [m]"),
        height=720,
        margin=dict(l=0, r=0, t=40, b=0),
    )
    return fig


def compute_app_result(inputs: Dict[str, Any]) -> Dict[str, Any]:
    axis_file = inputs["axis_file"]
    pc_h_file = inputs["pc_h_file"]
    pc_s_file = inputs["pc_s_file"]
    dem_source = inputs["dem_source"]
    dem_file = inputs.get("dem_file")
    opentopo_api_key = inputs.get("opentopo_api_key", "")
    opentopo_demtype = inputs.get("opentopo_demtype", "COP30")
    opentopo_margin_km = inputs.get("opentopo_margin_km", 3.0)
    profile_file = inputs.get("profile_file")
    curves_file = inputs.get("curves_file")
    sections_excel = inputs.get("sections_excel")
    manual_invert = inputs.get("manual_invert", False)
    profile_step = inputs.get("profile_step", 25.0)
    section_mode = inputs.get("section_mode", "Desde DEM natural")
    section_spacing = inputs.get("section_spacing", 100.0)
    half_width = inputs.get("half_width", 100.0)
    station_step = inputs.get("station_step", 5.0)
    prism_kind = inputs.get("prism_kind", "trapecial")
    bottom_width = inputs.get("bottom_width", 5.0)
    height = inputs.get("height", 2.0)
    talud_l = inputs.get("talud_l", 1.5)
    talud_r = inputs.get("talud_r", 1.5)
    manning_n = inputs.get("manning_n", 0.035)

    dem = None
    try:
        axis_coords, axis_name = pick_longest_line(axis_file, "eje del cauce")
        pc_h_lon, pc_h_lat, pc_h_z, pc_h_name = pick_first_point(pc_h_file, "PC hidrológico")
        pc_s_lon, pc_s_lat, pc_s_z, pc_s_name = pick_first_point(pc_s_file, "PC cuenca soporte")

        crs_proj = determine_local_utm_crs(pc_h_lon, pc_h_lat)
        to_proj, to_wgs = transformers_for_crs(crs_proj)
        axis_proj, axis_zs = coords_to_linestring_projected(axis_coords, to_proj)
        pc_h = point_projected(pc_h_lon, pc_h_lat, to_proj)
        pc_s = point_projected(pc_s_lon, pc_s_lat, to_proj)

        dem_download_info = None
        if dem_source == "Descargar desde OpenTopography":
            west, south, east, north = bbox_from_lonlat_inputs(axis_coords, pc_h_lon, pc_h_lat, pc_s_lon, pc_s_lat, opentopo_margin_km)
            area_bbox = estimate_bbox_area_km2(west, south, east, north)
            dem_download_info = {
                "demtype": opentopo_demtype,
                "west": west, "south": south, "east": east, "north": north,
                "area_bbox_km2_aprox": area_bbox,
                "margen_km": opentopo_margin_km,
            }
            dem_bytes = download_opentopography_dem(opentopo_api_key, opentopo_demtype, west, south, east, north)
            dem_file_runtime = BytesUpload(f"DEM_{opentopo_demtype}_opentopography.tif", dem_bytes)
            dem_size_mb = len(dem_bytes) / 1024 / 1024
        else:
            dem_file_runtime = dem_file
            dem_size_mb = None

        dem = DemSampler(dem_file_runtime) if dem_file_runtime is not None else None
        dem_sampler_func = None
        if dem is not None and dem.dataset is not None:
            dem_sampler_func = lambda x, y: dem.sample_xy(x, y, crs_proj)

        axis_oriented, inverted, direction_note, z_start_raw, z_end_raw = orient_axis_by_dem_or_user(axis_proj, dem_sampler_func, manual_invert)
        d_h = axis_oriented.project(pc_h)
        d_s = axis_oriented.project(pc_s)
        pc_h_on = axis_oriented.interpolate(d_h)
        pc_s_on = axis_oriented.interpolate(d_s)
        dist_pc_h = pc_h.distance(pc_h_on)
        dist_pc_s = pc_s.distance(pc_s_on)
        axis_useful = extract_subline(axis_oriented, d_h, d_s)

        respaldo_df = load_profile_file(profile_file) if profile_file is not None else pd.DataFrame()
        profile_df = generate_longitudinal_profile(axis_useful, dem, crs_proj, step_m=profile_step, respaldo=respaldo_df)
        warnings = []
        warnings.extend(evaluate_profile(profile_df))
        if dist_pc_h > 250:
            warnings.append(f"PC hidrológico está a {dist_pc_h:.1f} m del eje. Revisar proyección.")
        if dist_pc_s > 250:
            warnings.append(f"PC cuenca soporte está a {dist_pc_s:.1f} m del eje. Revisar proyección.")
        if axis_useful.length < 10:
            warnings.append("El tramo útil es demasiado corto.")
        if dem is None and section_mode == "Desde DEM natural":
            raise RuntimeError("Seleccionaste Desde DEM natural, pero no hay DEM disponible.")

        curves = load_support_curves(curves_file, to_proj) if curves_file is not None else []
        if section_mode == "Desde DEM natural":
            sections = generate_natural_sections(axis_useful, dem, crs_proj, section_spacing, half_width, station_step) if dem is not None else []
        elif section_mode == "Prismática por parámetros":
            sections = generate_prismatic_sections(axis_useful, profile_df, section_spacing, prism_kind, bottom_width, height, talud_l, talud_r, manning_n)
        else:
            sections = load_prismatic_sections_excel(sections_excel, axis_useful, profile_df) if sections_excel is not None else []
            if sections_excel is None:
                warnings.append("Modo Excel seleccionado, pero no se cargó Excel de secciones.")

        curve_eval = evaluate_curve_support(axis_useful, sections, curves) if sections else {"estado": "sin_secciones", "detalle": "No hay secciones para evaluar.", "secciones_cubiertas_pct": 0.0}

        validation = AxisValidation(
            axis_length_m=float(axis_oriented.length),
            useful_length_m=float(axis_useful.length),
            km_pc_hidrologico=float(d_h / 1000.0),
            km_pc_soporte=float(d_s / 1000.0),
            direction=direction_note,
            inverted=bool(inverted),
            left_right_defined=True,
            warnings=warnings,
        )
        return {
            "axis_oriented": axis_oriented,
            "axis_useful": axis_useful,
            "pc_h_on": pc_h_on,
            "pc_s_on": pc_s_on,
            "sections": sections,
            "profile_df": profile_df,
            "curves": curves,
            "curve_eval": curve_eval,
            "validation": validation,
            "to_wgs": to_wgs,
            "crs_proj": crs_proj,
            "warnings": warnings,
            "axis_name": axis_name,
            "pc_h_name": pc_h_name,
            "pc_s_name": pc_s_name,
            "pc_h_original_lonlat": [pc_h_lon, pc_h_lat],
            "pc_s_original_lonlat": [pc_s_lon, pc_s_lat],
            "dist_pc_h_m": dist_pc_h,
            "dist_pc_s_m": dist_pc_s,
            "section_mode": section_mode,
            "dem_source": dem_source,
            "dem_download_info": dem_download_info,
            "dem_size_mb": dem_size_mb,
        }
    finally:
        if dem is not None:
            dem.close()


def render_results(result: Dict[str, Any]):
    axis_oriented = result["axis_oriented"]
    axis_useful = result["axis_useful"]
    pc_h_on = result["pc_h_on"]
    pc_s_on = result["pc_s_on"]
    sections = result["sections"]
    profile_df = result["profile_df"]
    curves = result["curves"]
    curve_eval = result["curve_eval"]
    validation = result["validation"]
    to_wgs = result["to_wgs"]
    crs_proj = result["crs_proj"]
    warnings = result["warnings"]

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Etapa 1 · Eje",
        "Etapa 2 · Secciones",
        "Ventana sección por km",
        "3D interactivo / Corrección",
        "Descargas",
    ])

    with tab1:
        st.subheader("Validación del eje del cauce")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Longitud eje completo", f"{axis_oriented.length/1000:.3f} km")
        col2.metric("Longitud tramo útil", f"{axis_useful.length/1000:.3f} km")
        col3.metric("PC hidrológico sobre eje", f"km {validation.km_pc_hidrologico:.3f}")
        col4.metric("PC soporte sobre eje", f"km {validation.km_pc_soporte:.3f}")
        st.write(validation.direction)
        st.write("**Ribera izquierda/derecha:** definida mirando en sentido aguas arriba → aguas abajo del eje orientado.")
        if result.get("dem_download_info"):
            st.caption(f"DEM descargado: {result['dem_download_info']['demtype']} · {result.get('dem_size_mb', 0):.2f} MB · bbox aprox. {result['dem_download_info']['area_bbox_km2_aprox']:,.1f} km²")
        if warnings:
            st.warning("\n".join([f"- {w}" for w in warnings]))
        else:
            st.success("Eje útil validado sin advertencias críticas.")
        st.pyplot(plot_axis_sections(axis_oriented, axis_useful, pc_h_on, pc_s_on, sections, curves))
        st.subheader("Perfil longitudinal")
        st.pyplot(plot_profile(profile_df))
        figp = plot_profile_interactive(profile_df, sections)
        if figp is not None:
            st.plotly_chart(figp, use_container_width=True)
        st.dataframe(profile_df.head(300), use_container_width=True)

    with tab2:
        st.subheader("Resumen de secciones transversales")
        if not sections:
            st.error("No se generaron secciones. Revisa modo de generación e insumos.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("N° secciones", len(sections))
            c2.metric("Aceptadas", sum(1 for s in sections if s.status == "aceptada"))
            c3.metric("Corregidas", sum(1 for s in sections if s.status == "corregida"))
            c4.metric("Revisar/Rechazadas", sum(1 for s in sections if s.status not in ["aceptada", "corregida"]))
            st.write(f"**Evaluación curvas de apoyo:** {curve_eval['estado']} · {curve_eval['detalle']}")
            df_secs = pd.DataFrame([{
                "índice": i,
                "km": s.km,
                "source": s.source,
                "status": s.status,
                "n_puntos": len(s.stations),
                "cota_min": float(np.nanmin(s.elevations)) if np.isfinite(np.array(s.elevations, dtype=float)).any() else np.nan,
                "cota_max": float(np.nanmax(s.elevations)) if np.isfinite(np.array(s.elevations, dtype=float)).any() else np.nan,
                "notes": s.notes,
            } for i, s in enumerate(sections)])
            st.dataframe(df_secs, use_container_width=True, height=450)

    with tab3:
        st.subheader("Ventana independiente de sección por km")
        if not sections:
            st.info("No hay secciones para visualizar.")
        else:
            labels = [f"km {s.km:.3f} · {s.status} · #{i}" for i, s in enumerate(sections)]
            current_idx = int(st.selectbox("Seleccionar sección por km", options=list(range(len(sections))), format_func=lambda i: labels[i], key="section_select_idx"))
            sec = sections[current_idx]
            col_a, col_b = st.columns([1.2, 1.0])
            with col_a:
                st.pyplot(plot_single_section(sec))
                st.caption("La estación va de ribera izquierda a ribera derecha, mirando aguas abajo.")
            with col_b:
                st.markdown(f"**Sección km {sec.km:.3f}**")
                st.write(f"Fuente: `{sec.source}`")
                new_status = st.selectbox("Estado", ["aceptada", "corregida", "revisar", "rechazada"], index=["aceptada", "corregida", "revisar", "rechazada"].index(sec.status) if sec.status in ["aceptada", "corregida", "revisar", "rechazada"] else 2, key=f"status_{current_idx}")
                new_notes = st.text_area("Observación", value=sec.notes or "", key=f"notes_{current_idx}", height=90)
                edit_df = st.data_editor(
                    section_to_edit_df(sec),
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"editor_section_{current_idx}",
                    column_config={
                        "station_m": st.column_config.NumberColumn("Station [m]", step=0.1),
                        "cota_m": st.column_config.NumberColumn("Cota [m]", step=0.01),
                    },
                )
                c1, c2, c3, c4 = st.columns(4)
                if c1.button("Aplicar edición", key=f"apply_{current_idx}"):
                    try:
                        sections[current_idx] = apply_section_edit(sec, edit_df, new_status, new_notes)
                        result["sections"] = sections
                        st.session_state["eje_result"] = result
                        st.success("Sección actualizada.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if c2.button("Suavizar", key=f"smooth_{current_idx}"):
                    sections[current_idx] = smooth_section(sec)
                    result["sections"] = sections
                    st.session_state["eje_result"] = result
                    st.rerun()
                if c3.button("Interpolar", key=f"interp_{current_idx}", disabled=(current_idx == 0 or current_idx == len(sections)-1)):
                    out = interpolate_section_from_neighbors(sections, current_idx)
                    if out is not None:
                        sections[current_idx] = out
                        result["sections"] = sections
                        st.session_state["eje_result"] = result
                        st.rerun()
                if c4.button("Rechazar", key=f"reject_{current_idx}"):
                    sec.status = "rechazada"
                    sec.notes = (sec.notes or "") + " | rechazada por revisión"
                    sections[current_idx] = sec
                    result["sections"] = sections
                    st.session_state["eje_result"] = result
                    st.rerun()

    with tab4:
        st.subheader("Perfil longitudinal del cauce con secciones en 3D interactivo")
        fig3d = plot_sections_3d_interactive(axis_useful, profile_df, sections)
        if fig3d is not None and sections:
            st.plotly_chart(fig3d, use_container_width=True)
        else:
            st.info("Vista 3D no disponible. Verifica que Plotly esté instalado y que existan secciones.")
        st.markdown(
            """
**Uso recomendado de revisión:**
1. Recorre el perfil longitudinal 3D y detecta secciones muy planas, invertidas o con saltos de cota.
2. Abre la pestaña **Ventana sección por km**.
3. Edita cotas station-elevación, suaviza o interpola entre secciones vecinas.
4. Descarga nuevamente Excel/KMZ; las descargas usan las secciones ya modificadas.
"""
        )

    with tab5:
        st.subheader("Descargas actualizadas")
        kmz_bytes = make_kmz(axis_oriented, axis_useful, pc_h_on, pc_s_on, sections, to_wgs)
        st.download_button("Descargar KMZ eje + secciones", kmz_bytes, file_name="hidrosed_eje_cauce_secciones_v4_editado.kmz", mime="application/vnd.google-earth.kmz")
        st.download_button("Descargar perfil longitudinal CSV", profile_df.to_csv(index=False).encode("utf-8"), file_name="perfil_longitudinal_tramo_util.csv", mime="text/csv")
        st.download_button("Descargar Excel tipo HEC-RAS actualizado", sections_to_hecras_excel(sections), file_name="secciones_tipo_hecras_editadas.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        summary = {
            "modulo": "HidroSed Eje Cauce y Secciones v4",
            "crs_proyectado": crs_proj.to_string(),
            "axis_validation": asdict(validation),
            "pc_hidrologico_original_lonlat": result.get("pc_h_original_lonlat"),
            "pc_soporte_original_lonlat": result.get("pc_s_original_lonlat"),
            "dist_pc_hidrologico_a_eje_m": result.get("dist_pc_h_m"),
            "dist_pc_soporte_a_eje_m": result.get("dist_pc_s_m"),
            "numero_secciones": len(sections),
            "secciones_por_estado": pd.Series([s.status for s in sections]).value_counts().to_dict() if sections else {},
            "evaluacion_curvas_apoyo": curve_eval,
            "modo_secciones": result.get("section_mode"),
            "fuente_dem": result.get("dem_source"),
            "descarga_opentopography": result.get("dem_download_info"),
        }
        st.download_button("Descargar JSON técnico actualizado", json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"), file_name="resumen_tecnico_eje_secciones_v4.json", mime="application/json")


# --------------------------------------------------------------------------------------
# App UI
# --------------------------------------------------------------------------------------
st.title("🌊 HidroSed · Módulo Eje Cauce y Secciones v4")
st.caption("Eje útil obligatorio · DEM OpenTopography/manual · visor de sección por km · edición de secciones · perfil longitudinal 3D interactivo")

with st.expander("Criterio técnico del módulo", expanded=False):
    st.markdown(
        """
Este módulo construye y revisa el insumo hidráulico del cauce:

- eje completo y eje útil entre PC hidrológico y PC cuenca soporte;
- sentido aguas arriba → aguas abajo;
- ribera izquierda y derecha mirando aguas abajo;
- DEM descargado desde OpenTopography o GeoTIFF cargado por el usuario;
- perfil longitudinal DEM y perfil topográfico de respaldo opcional;
- secciones transversales naturales o prismáticas;
- **ventana independiente de revisión por km**;
- **edición de secciones** antes de exportar;
- **perfil longitudinal 3D interactivo con secciones**.
"""
    )

st.sidebar.header("1. Entradas obligatorias")
axis_file = st.sidebar.file_uploader("Eje del cauce obligatorio · KMZ/KML con línea", type=["kmz", "kml"])
pc_h_file = st.sidebar.file_uploader("PC hidrológico · KMZ/KML con punto", type=["kmz", "kml"])
pc_s_file = st.sidebar.file_uploader("PC cuenca soporte · KMZ/KML con punto", type=["kmz", "kml"])

st.sidebar.header("2. DEM")
st.sidebar.markdown("El DEM puede cargarse manualmente como GeoTIFF o descargarse automáticamente desde **OpenTopography** usando API Key.")
dem_source = st.sidebar.radio("Fuente DEM", ["Descargar desde OpenTopography", "Cargar DEM GeoTIFF manual"], index=0)
dem_file = None
opentopo_api_key = ""
opentopo_demtype = "COP30"
opentopo_margin_km = 3.0
if dem_source == "Cargar DEM GeoTIFF manual":
    dem_file = st.sidebar.file_uploader("DEM GeoTIFF · cargar aquí", type=["tif", "tiff"])
else:
    opentopo_demtype = st.sidebar.selectbox("DEM OpenTopography", ["COP30", "NASADEM", "SRTMGL1", "SRTMGL3", "AW3D30"], index=0)
    opentopo_api_key = st.sidebar.text_input("API Key OpenTopography", type="password", help="La API Key no se guarda ni se imprime en archivos de salida.")
    opentopo_margin_km = st.sidebar.number_input("Margen de descarga DEM alrededor del eje y PCs [km]", min_value=0.5, max_value=100.0, value=3.0, step=0.5)

st.sidebar.header("3. Entradas opcionales de respaldo")
profile_file = st.sidebar.file_uploader("Perfil topográfico longitudinal de respaldo opcional", type=["csv", "txt", "xlsx", "xls", "kmz", "kml", "dxf"])
curves_file = st.sidebar.file_uploader("Curvas de nivel de apoyo opcionales · KMZ/KML/DXF", type=["kmz", "kml", "dxf"])
sections_excel = st.sidebar.file_uploader("Excel de secciones prismáticas opcional", type=["xlsx", "xls"])

st.sidebar.header("4. Parámetros eje")
manual_invert = st.sidebar.checkbox("Invertir sentido del eje manualmente", value=False)
profile_step = st.sidebar.number_input("Paso perfil longitudinal DEM [m]", min_value=5.0, max_value=500.0, value=25.0, step=5.0)

st.sidebar.header("5. Parámetros secciones")
section_mode = st.sidebar.selectbox("Modo de generación de secciones", ["Desde DEM natural", "Prismática por parámetros", "Desde Excel prismático"])
section_spacing = st.sidebar.number_input("Separación entre secciones s [m]", min_value=5.0, max_value=1000.0, value=100.0, step=5.0)
half_width = st.sidebar.number_input("Semi-ancho de sección natural [m]", min_value=5.0, max_value=2000.0, value=100.0, step=5.0)
station_step = st.sidebar.number_input("Paso de muestreo transversal [m]", min_value=1.0, max_value=100.0, value=5.0, step=1.0)

if section_mode == "Desde DEM natural":
    if dem_source == "Cargar DEM GeoTIFF manual" and dem_file is None:
        st.sidebar.warning("Para usar secciones naturales debes cargar un DEM GeoTIFF.")
    if dem_source == "Descargar desde OpenTopography" and not opentopo_api_key:
        st.sidebar.warning("Para usar secciones naturales con OpenTopography debes ingresar API Key.")

with st.sidebar.expander("Parámetros cauce prismático"):
    prism_kind = st.selectbox("Tipo", ["trapecial", "rectangular"])
    bottom_width = st.number_input("Ancho de fondo [m]", min_value=0.1, max_value=500.0, value=5.0, step=0.5)
    height = st.number_input("Altura [m]", min_value=0.1, max_value=50.0, value=2.0, step=0.1)
    talud_l = st.number_input("Talud izquierdo H:V", min_value=0.0, max_value=20.0, value=1.5, step=0.1)
    talud_r = st.number_input("Talud derecho H:V", min_value=0.0, max_value=20.0, value=1.5, step=0.1)
    manning_n = st.number_input("Manning n", min_value=0.010, max_value=0.200, value=0.035, step=0.001, format="%.3f")

st.sidebar.download_button(
    "Descargar plantilla Excel secciones prismáticas",
    data=make_prismatic_template(),
    file_name="plantilla_secciones_prismaticas_hidrosed.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

if st.sidebar.button("Limpiar resultados guardados"):
    st.session_state.pop("eje_result", None)
    st.rerun()

run = st.sidebar.button("Procesar eje y secciones", type="primary")

if run:
    if axis_file is None or pc_h_file is None or pc_s_file is None:
        st.error("Faltan entradas obligatorias: eje del cauce, PC hidrológico y PC cuenca soporte.")
        st.stop()
    try:
        inputs = dict(
            axis_file=axis_file,
            pc_h_file=pc_h_file,
            pc_s_file=pc_s_file,
            dem_source=dem_source,
            dem_file=dem_file,
            opentopo_api_key=opentopo_api_key,
            opentopo_demtype=opentopo_demtype,
            opentopo_margin_km=opentopo_margin_km,
            profile_file=profile_file,
            curves_file=curves_file,
            sections_excel=sections_excel,
            manual_invert=manual_invert,
            profile_step=profile_step,
            section_mode=section_mode,
            section_spacing=section_spacing,
            half_width=half_width,
            station_step=station_step,
            prism_kind=prism_kind,
            bottom_width=bottom_width,
            height=height,
            talud_l=talud_l,
            talud_r=talud_r,
            manning_n=manning_n,
        )
        with st.spinner("Procesando eje, DEM, perfil y secciones..."):
            result = compute_app_result(inputs)
        st.session_state["eje_result"] = result
        st.success("Procesamiento finalizado. Puedes revisar y editar las secciones sin reprocesar el DEM.")
    except Exception as e:
        st.error("No se pudo procesar la información.")
        st.exception(e)
        st.stop()

if "eje_result" not in st.session_state:
    st.info("Carga el eje del cauce, PC hidrológico y PC cuenca soporte. Luego elige DEM OpenTopography o GeoTIFF manual y presiona **Procesar eje y secciones**.")
    st.stop()

render_results(st.session_state["eje_result"])
