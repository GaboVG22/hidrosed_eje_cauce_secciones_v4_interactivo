# HidroSed · Módulo Eje Cauce y Secciones v4

Aplicación Streamlit independiente para construir, revisar y corregir el eje hidráulico útil y las secciones transversales antes de integrarlo en HidroSed.

## Cambio principal v4

Se agregan las funciones que faltaban en v3:

- Ventana independiente para visualizar cada sección por **km**.
- Selector de sección por km.
- Editor station-elevation por sección.
- Cambio de estado de sección: aceptada, corregida, revisar o rechazada.
- Botones de corrección rápida: suavizar, interpolar entre vecinas y rechazar.
- Perfil longitudinal interactivo con ubicación de secciones.
- Vista 3D interactiva del eje útil con secciones transversales.
- Las descargas KMZ, Excel tipo HEC-RAS y JSON usan las secciones ya editadas.
- Los resultados quedan guardados en `session_state`, por lo que la revisión de secciones no reprocesa el DEM ni vuelve a descargar desde OpenTopography.

## Objetivo

Resolver tres etapas:

1. **Eje del cauce**
   - Ingreso obligatorio de eje en KMZ/KML.
   - Ingreso de PC hidrológico y PC cuenca soporte.
   - Definición del sentido aguas arriba → aguas abajo.
   - Definición de ribera izquierda y derecha mirando aguas abajo.
   - Recorte del eje útil entre PC hidrológico y PC cuenca soporte.
   - Perfil longitudinal DEM y perfil topográfico de respaldo opcional.

2. **Secciones del cauce**
   - Generación de secciones naturales desde DEM.
   - Generación de secciones trapeciales o rectangulares.
   - Carga de Excel de secciones prismáticas.
   - Carga de curvas de nivel de apoyo en KMZ/KML/DXF.
   - Evaluación de cobertura de curvas a ambos lados del eje.

3. **Eje + secciones en 3D y corrección**
   - Vista 2D.
   - Ventana de sección por km.
   - Edición station-elevation.
   - Vista 3D interactiva.
   - Exportación KMZ, Excel tipo HEC-RAS, perfil CSV y JSON técnico.

## Inputs obligatorios

- Eje del cauce: KMZ/KML con línea.
- PC hidrológico: KMZ/KML con punto.
- PC cuenca soporte: KMZ/KML con punto.

## Entrada DEM

La app permite dos formas:

### A. Descargar desde OpenTopography

- Ingresar API Key de OpenTopography.
- Seleccionar DEM, recomendado: `COP30`.
- Definir margen de descarga alrededor del eje y puntos de control.
- La API Key no se guarda ni se imprime en archivos de salida.

### B. Cargar DEM manual

- DEM GeoTIFF: `.tif` o `.tiff`.
- Puede ser COP30, NASADEM, SRTM u otro DEM compatible.

El DEM es obligatorio para generar secciones naturales desde terreno.

## Inputs opcionales

- Perfil longitudinal de respaldo: CSV, TXT, Excel, KMZ/KML o DXF.
- Curvas de nivel de apoyo: KMZ/KML/DXF.
- Excel de secciones prismáticas.

## Despliegue Streamlit Cloud

- Main file path: `app.py`
- Python version: `3.11`

## Dependencias

La app evita dependencias problemáticas para Streamlit Cloud como `pysheds`, `geopandas`, `scipy` y `earthengine-api`.

## Recomendación de primera prueba

- DEM: COP30 desde OpenTopography.
- Margen DEM: 3 km.
- Separación secciones: 100 m.
- Semi-ancho sección natural: 100 m.
- Paso transversal: 5 m.

## Flujo de trabajo recomendado

1. Procesar eje y secciones.
2. Revisar perfil longitudinal.
3. Abrir “Ventana sección por km”.
4. Editar o marcar las secciones dudosas.
5. Revisar la vista 3D interactiva.
6. Descargar KMZ y Excel HEC-RAS actualizados.
