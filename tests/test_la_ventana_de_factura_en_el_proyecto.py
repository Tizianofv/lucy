"""LA VENTANA DE UNA FACTURA ENCIMA DE LA PÁGINA DEL PROYECTO (23-oct-2026).

Pedido de Tiziano, viendo la ventana publicada: «no esta bien no mira como se ve cuando le doy a +» (el paso 2 salía con
la casilla sola y el texto en una columna de una palabra por línea) e «incluso se va de proyectos y quiero que sea igual
que cotizaciones que se queda en la misma pagina».

Qué se vigila aquí, por las RUTAS reales con `TestClient` sobre la base de SQLite de `test_guardar_factura` con las tablas
de los proyectos encima (`test_pantalla_de_facturas.con_proyecto`) y la App de registro doblada EN LA RED
(`test_pantalla_de_facturas.registro`):

  1. ABRIRLA DESDE UN PROYECTO SIRVE LA PÁGINA DEL PROYECTO con la ventana encima — no la página de `/facturas/nueva`;
  2. LOS TRES PASOS se quedan en la página del proyecto: los GET van a `/proyectos` con `p` y `factura`, y el POST de
     guardar es el de siempre (`/facturas/nueva`, mismas puertas);
  3. CERRAR (✕ y «Cancelar») vuelve al mismo proyecto, en su bloque de facturas;
  4. «CORREGIR» de un borrador DEL proyecto se abre encima, con el POST de siempre; una factura de otro proyecto, una
     emitida o un id que no es número NO abren nada;
  5. SOLO VER no la ve (no se arma: escribe);
  6. EL PASO 2 SE VE BIEN: con dos sesiones, la regla CSS que de verdad se aplica a la casilla y al texto no aplasta el
     texto (la casilla con su ancho, el texto con el resto de la fila). Se calcula la cascada sobre el CSS que la página
     sirve de verdad, no se busca un texto.

FRONTERA: el aspecto final (colores, tamaños, que se vea bonito) se mira en un navegador; aquí se mide la cascada de las
dos propiedades que aplastaban el texto. El «sin saltar» y los censos de controles de `test_proyectos_solo_ver.py` /
`test_pagina_sin_saltos.py` leen la plantilla de `proyectos.html` y sus incluidas directas: la ventana vive en su macro
(`_ventana_factura_nueva.html`), que esos censos NO leen. Lo que la ventana pinta lo vigila este archivo.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser

import pytest

import test_guardar_factura as tgf                     # primero: variables de entorno
from test_pantalla_de_facturas import cliente, con_proyecto, registro, _factura_en_proyecto, _emitida_por_sql  # noqa: F401
import test_pantalla_de_facturas as tpf

import noco_lectura


@pytest.fixture(autouse=True)
def _noco(monkeypatch):
    """La ficha 7 se llama como en `test_pantalla_de_facturas` (sin esto la ventana sale igual, pero diciendo que Noco
    no contestó, y lo que se mide aquí no es eso)."""
    async def persona(noco_id):
        return {"id": noco_id, "nombre": tpf.NOMBRE} if noco_id == 7 else None

    async def buscar(texto):
        return [{"id": 7, "nombre": tpf.NOMBRE}] if "cli" in texto.lower() else []

    monkeypatch.setattr(noco_lectura, "persona", persona)
    monkeypatch.setattr(noco_lectura, "buscar_personas", buscar)


def _pagina(m, con="casa", **consulta) -> str:
    r = cliente(con).get("/proyectos", params=consulta)
    assert r.status_code == 200, (consulta, r.status_code, r.text[:400])
    return r.text


ABRE_LA_VENTANA = '<div class="velo" role="dialog"'


def _lo_que_manda_el_formulario(html: str, marca: str) -> dict:
    """El cuerpo que la persona enviaría, leído del FORMULARIO QUE LA VENTANA PINTA (`marca` es su atributo identificador:
    `data-factura-nueva` o `data-corregir`): cada casilla con el valor que trae el HTML, las de marcar solo si vienen
    marcadas, `ref` repetido como lista y el texto de los `<textarea>`. Nada se teclea aquí."""
    bloque = re.search(r"<form[^>]*" + marca + r"[^>]*>(.*?)</form>", html, re.S)
    assert bloque, f"la ventana no trae el formulario {marca}"
    datos: dict = {}
    for etiqueta in re.findall(r"<input\b[^>]*>", bloque.group(1)):
        a = {k: html_lib.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', etiqueta)}
        nombre = a.get("name")
        if not nombre:
            continue
        if a.get("type") in ("checkbox", "radio"):
            if "checked" in etiqueta:
                datos[nombre] = a.get("value", "on")
            continue
        if nombre == "ref":
            datos.setdefault("ref", []).append(a.get("value", ""))
        else:
            datos[nombre] = a.get("value", "")
    for etiqueta, texto in re.findall(r"<textarea\b([^>]*)>(.*?)</textarea>", bloque.group(1), re.S):
        a = {k: html_lib.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', etiqueta)}
        if a.get("name"):
            datos[a["name"]] = html_lib.unescape(texto)
    return datos


def _ventana(html: str) -> str:
    """Lo que hay a partir del `<div class="velo">` de la ventana: para mirar la ventana y no la página. La marca lleva el
    `<div` y el `role`: el CSS de la hoja nombra `.velo{` y eso no es una ventana abierta."""
    assert ABRE_LA_VENTANA in html, "la página no trae ninguna ventana"
    return html[html.index(ABRE_LA_VENTANA):]


# ── 1. La página que se sirve ES la del proyecto ────────────────────────────────────────────────────

def test_abierta_desde_un_proyecto_la_pagina_es_la_del_proyecto_con_la_ventana_encima(con_proyecto, registro):
    html = _pagina(con_proyecto, p=1, factura="nueva")
    # La página del proyecto, entera: sus dos regiones, su bloque de facturas y el proyecto escogido.
    assert 'data-region="izquierda"' in html and 'data-region="centro"' in html
    assert 'id="facturas-del-proyecto"' in html and "Disco Uno" in html
    # Y NO es la página suelta de la factura (la que se sirve en `/facturas/nueva`): esa lleva `data-region="cuerpo"`
    # (la de `base.html`) y no tiene la columna de grupos.
    assert 'data-region="cuerpo"' not in html
    # La ventana, encima, con su velo y su marco de siempre.
    ventana = _ventana(html)
    assert 'role="dialog"' in ventana and 'class="cz-ventana"' in ventana
    assert "2. ¿De cuáles sesiones o trabajos?" in ventana          # el proyecto 1 tiene cliente: entra por el paso 2


# ── 2. Los tres pasos, y el POST de siempre ─────────────────────────────────────────────────────────

def test_los_tres_pasos_se_quedan_en_la_pagina_del_proyecto(con_proyecto, registro):
    # Paso 1: un proyecto sin cliente abre la búsqueda en Noco, y «Escoger» se queda en la página del proyecto.
    con_proyecto.proyecto(9, "Sin cliente todavía", area="CDS")
    paso1 = _ventana(_pagina(con_proyecto, p=9, factura="nueva", pq="cli"))
    assert "1. ¿De quién es la factura?" in paso1
    assert re.search(r'<form method="get" action="/proyectos"[^>]*>', paso1), "el paso 1 no vuelve a la página"
    assert 'name="p" value="9"' in paso1 and 'name="factura" value="nueva"' in paso1
    assert 'href="/proyectos?cliente=7&amp;p=9&amp;factura=nueva"' in paso1

    # Paso 2: el cliente del proyecto, sus sesiones y trabajos; el formulario que continúa y busca vuelve a /proyectos.
    paso2 = _ventana(_pagina(con_proyecto, p=1, factura="nueva"))
    assert "2. ¿De cuáles sesiones o trabajos?" in paso2
    forma = re.search(r'<form method="get" action="/proyectos"[^>]*data-escoger[^>]*>', paso2)
    assert forma, "el paso 2 no se queda en la página del proyecto"
    assert '<button name="paso" value="escribir" class="btn-prim">' in paso2
    assert 'name="p" value="1"' in paso2 and 'name="factura" value="nueva"' in paso2

    # Paso 3: lo escrito; el POST es el de SIEMPRE (`/facturas/nueva`, la misma puerta).
    paso3 = _ventana(_pagina(con_proyecto, p=1, factura="nueva", ref=["11"], paso="escribir"))
    assert "3. Escribe la factura" in paso3
    assert '<form method="post" action="/facturas/nueva" data-factura-nueva>' in paso3
    assert 'name="proyecto" value="1"' in paso3                     # las casillas escondidas del POST no cambian
    # Y los pasos NO se van a la página suelta de la factura.
    assert 'href="/facturas/nueva?' not in paso2 and 'action="/facturas/nueva"' not in paso2


def _el_paso_3(con_proyecto, refs=("11",)):
    """El HTML del paso 3 con la ventana encima del proyecto, y lo que manda su formulario."""
    html = _pagina(con_proyecto, p=1, factura="nueva", ref=list(refs), paso="escribir")
    return html, _lo_que_manda_el_formulario(html, "data-factura-nueva")


def test_guardar_desde_la_ventana_del_proyecto_crea_el_borrador_y_vuelve_al_proyecto(con_proyecto, registro):
    """Con el formulario que PINTA la ventana (no un cuerpo armado aparte): la ruta es la de siempre y el borrador queda
    ligado al proyecto; desde el proyecto, la vuelta es a la página del proyecto con el aviso, como las cotizaciones."""
    _, datos = _el_paso_3(con_proyecto)
    assert datos["volver_al_proyecto"] == "1" and datos["proyecto"] == "1"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    creada = tgf.filas(con_proyecto, "facturas")[0]
    assert r.status_code == 303, r.text[:300]
    assert r.headers["location"] == f"/proyectos?hecho=factura_guardada&p=1&factura={creada['id']}#facturas"
    assert creada["proyecto_id"] == 1 and creada["estado"] == "borrador"
    # Y al seguir la vuelta, la página del proyecto con el aviso (la misma que daría la ventana de cotizaciones).
    html = cliente().get(r.headers["location"]).text
    assert 'data-region="centro"' in html and 'data-factura-guardada' in html
    assert "Borrador guardado en este proyecto." in html


def test_una_factura_del_proyecto_se_corrige_desde_su_ventana_y_vuelve_al_proyecto(con_proyecto, registro):
    # El `now()` de esta base de prueba devuelve "…T12:00:00+00:00" y el candado de `db` compara contra el instante ya
    # pasado por el adaptador de sqlite ("… 12:00:00+00:00"): con la T, la comparación no cuadra NUNCA y todo rechazo
    # sería «la factura cambió». Se deja el reloj en el formato que el adaptador escribe (es un artefacto de la base de
    # prueba, no del camino que se mide): las rutas y la puerta son las de verdad.
    con_proyecto.con.create_function("now", 0, lambda: "2026-10-02 12:00:00+00:00")
    f = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    html = _pagina(con_proyecto, p=1, factura=f["id"], corregir=1)
    datos = _lo_que_manda_el_formulario(html, "data-corregir")
    assert datos["volver_al_proyecto"] == "1"
    datos["para_nombre"] = "Nombre corregido"
    r = cliente().post(f"/facturas/{f['id']}/corregir", data=datos, follow_redirects=False)
    assert r.status_code == 303, re.findall(r'<p class="aviso[^"]*"[^>]*>(.*?)</p>', r.text, re.S)[:3]
    assert r.headers["location"] == f"/proyectos?hecho=factura_guardada&p=1&factura={f['id']}#facturas"
    assert tgf.filas(con_proyecto, "facturas")[0]["para_nombre"] == "Nombre corregido"


def test_un_rechazo_desde_la_ventana_del_proyecto_vuelve_a_la_ventana_con_el_aviso_y_lo_escrito(con_proyecto, registro):
    """Un NCF sin ITBIS lo rechaza la puerta de `db` (no el navegador): la página sigue siendo la del proyecto, con la
    ventana encima, la razón dentro y lo que la persona escribió."""
    _, datos = _el_paso_3(con_proyecto)
    datos["ncf"] = "B0100000001"          # con comprobante y sin marcar el ITBIS: `ncf_sin_itbis`
    datos["para_nombre"] = "Nombre que no se pierde"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 400
    html = r.text
    assert 'data-region="centro"' in html and 'data-region="cuerpo"' not in html
    assert ABRE_LA_VENTANA in html and 'data-error-de-guardado' in html
    assert "ITBIS" in html
    assert 'value="Nombre que no se pierde"' in html
    assert not tgf.filas(con_proyecto, "facturas")     # nada escrito


def test_un_rechazo_antes_de_guardar_tambien_se_queda_en_el_proyecto(con_proyecto, registro):
    """Sin sesiones la ruta lo rechaza ANTES de tocar la base (otra salida del rechazo, la de arriba del formulario)."""
    _, datos = _el_paso_3(con_proyecto)
    datos.pop("ref")
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 400
    assert 'data-region="centro"' in r.text and ABRE_LA_VENTANA in r.text
    assert "Escoge al menos una sesión o un trabajo para la factura." in r.text
    assert not tgf.filas(con_proyecto, "facturas")


def test_pedir_emision_desde_la_ventana_del_proyecto_pinta_la_confirmacion_encima(con_proyecto, registro):
    _, datos = _el_paso_3(con_proyecto)
    datos["accion"] = "pedir_emision"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 200
    html = r.text
    assert 'data-region="centro"' in html and ABRE_LA_VENTANA in html
    assert "data-confirmar-emision" in html and "data-emitir" in html
    assert "¿Emitir esta factura?" in html
    assert not tgf.filas(con_proyecto, "facturas")     # pedir no guarda


def test_volver_desde_la_confirmacion_de_emitir_se_queda_en_la_ventana_del_proyecto(con_proyecto, registro):
    _, datos = _el_paso_3(con_proyecto)
    datos["accion"] = "volver"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 200 and 'data-region="centro"' in r.text
    assert "3. Escribe la factura" in r.text and 'value="' + datos["para_nombre"] + '"' in r.text
    assert not tgf.filas(con_proyecto, "facturas")


def test_emitir_desde_la_ventana_del_proyecto_vuelve_al_proyecto(con_proyecto, registro, monkeypatch):
    """La emisión DE VERDAD (la puerta `db.emitir_factura` con su candado) la ejerce
    `tests/test_emitir_desde_la_ventana.py` sobre la MISMA ruta y la base que tiene el reloj que su candado pide; aquí se
    dobla solo para medir ADÓNDE vuelve, que es lo que cambió."""
    import db.db as db

    async def emitir_doblado(fid, chat, cambiado_en):
        con_proyecto.con.execute("UPDATE facturas SET estado = 'emitida', anio = 2026, numero = 1, "
                                 "emitida_en = '2026-10-21T12:00:00+00:00' WHERE id = ?", (fid,))
        con_proyecto.con.commit()
        return await db.factura_por_id(fid)

    monkeypatch.setattr(db, "emitir_factura", emitir_doblado)
    _, datos = _el_paso_3(con_proyecto)
    datos["accion"] = "emitir"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    creada = tgf.filas(con_proyecto, "facturas")[0]
    assert r.status_code == 303
    assert r.headers["location"] == f"/proyectos?hecho=factura_guardada&p=1&factura={creada['id']}#facturas"
    html = cliente().get(r.headers["location"]).text
    assert "Factura FAC-2026-001 emitida." in html


def test_sin_el_campo_escondido_la_ventana_vuelve_a_la_pagina_de_la_factura(con_proyecto, registro):
    """El destino no es libre: sin el campo escondido (o con otro proyecto) el POST vuelve a donde volvía antes."""
    _, datos = _el_paso_3(con_proyecto)
    datos.pop("volver_al_proyecto")
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/facturas/")
    # Y un campo que NO es el proyecto de la factura se ignora igual (la otra sesión, que la 11 ya está facturada).
    _, datos = _el_paso_3(con_proyecto, refs=("12",))
    datos["volver_al_proyecto"] = "2"
    r = cliente().post("/facturas/nueva", data=datos, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/facturas/")


CONTENIDO_DE_DOS_OPCIONES = json.dumps(
    {"opciones": [{"nombre": "A", "renglones": [{"descripcion": "Mezcla", "detalle": "", "total": "1000.00"}]},
                  {"nombre": "B", "renglones": [{"descripcion": "Master", "detalle": "", "total": "2000.00"}]}],
     "condiciones": []})


def test_la_opcion_de_una_cotizacion_se_escoge_en_el_paso_2_y_llega_al_paso_3(con_proyecto, registro):
    """La cotización con más de una opción: el `<select>` del paso 2 tiene que mandar el nombre que la ruta lee
    (`factura_opcion`, no `opcion`), una sola vez, y la opción escogida tiene que llegar al paso 3 y al POST."""
    import test_cotizaciones_tablas as ct
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=1, estado="enviada", enviada_en="2026-10-14T12:00:00+00:00",
                 contenido=CONTENIDO_DE_DOS_OPCIONES)
    ventana = _ventana(_pagina(con_proyecto, p=1, factura="nueva", factura_de_cotizacion=1))
    assert '<select name="factura_opcion"' in ventana, "el paso 2 manda la opción con un nombre que la ruta no lee"
    forma = re.search(r"<form[^>]*data-escoger[^>]*>(.*?)</form>", ventana, re.S).group(1)
    assert forma.count('name="factura_opcion"') == 1, "dos valores del mismo nombre se pisan: el escondido y el select"
    # Escoger la segunda y seguir: la opción viaja y el POST del paso 3 la lleva como `opcion` (la que lee la puerta).
    paso3 = _ventana(_pagina(con_proyecto, p=1, factura="nueva", factura_de_cotizacion=1, factura_opcion=2,
                             ref=["11"], paso="escribir"))
    assert "3. Escribe la factura" in paso3
    datos = _lo_que_manda_el_formulario(paso3, "data-factura-nueva")
    assert datos["opcion"] == "2" and datos["cotizacion"] == "1"


def test_desde_una_cotizacion_la_ventana_nace_de_ella_y_se_queda_en_el_proyecto(con_proyecto, registro):
    import test_cotizaciones_tablas as ct
    import test_pantalla_de_facturas as tp
    ct._insertar(con_proyecto.con, proyecto_id=1, numero=1, estado="enviada",
                 enviada_en="2026-10-14T12:00:00+00:00", contenido=tp.CONTENIDO)
    html = _pagina(con_proyecto, p=1, factura="nueva", factura_de_cotizacion=1)
    ventana = _ventana(html)
    assert "Sale de la cotización" in ventana and "COT-2026-001" in ventana
    assert 'name="factura_de_cotizacion" value="1"' in ventana      # el paso que sigue la sigue llevando


# ── 3. Cerrar vuelve al proyecto ────────────────────────────────────────────────────────────────────

def test_cerrar_y_cancelar_vuelven_al_proyecto(con_proyecto, registro):
    # La ✕ de la cabecera, en los pasos 1 y 2 (esos no tienen pie con «Cancelar»).
    for consulta in ({"p": 1, "factura": "nueva"},
                     {"p": 1, "factura": "nueva", "ref": ["11"], "paso": "escribir"}):
        ventana = _ventana(_pagina(con_proyecto, **consulta))
        cerrar = re.search(r'<a class="cerrar" href="([^"]+)"', ventana).group(1)
        assert cerrar == "/proyectos?p=1#facturas", consulta
    # «Cancelar», en el paso 3 (el que tiene pie con los botones de guardar).
    paso3 = _ventana(_pagina(con_proyecto, p=1, factura="nueva", ref=["11"], paso="escribir"))
    assert '<a class="btn-linea" href="/proyectos?p=1#facturas">Cancelar</a>' in paso3


# ── 4. Corregir un borrador del proyecto ────────────────────────────────────────────────────────────

def test_corregir_un_borrador_del_proyecto_se_abre_encima(con_proyecto, registro):
    f = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    html = _pagina(con_proyecto, p=1, factura=f["id"], corregir=1)
    assert 'data-region="centro"' in html                            # la página del proyecto, otra vez
    ventana = _ventana(html)
    assert "Corregir el borrador" in ventana
    assert f'action="/facturas/{f["id"]}/corregir"' in ventana       # la ruta de siempre
    assert 'name="cambiado_en"' in ventana                           # el candado que lee la puerta
    assert '<a class="btn-linea" href="/proyectos?p=1#facturas">Cancelar</a>' in ventana
    assert re.search(r'<a class="cerrar" href="/proyectos\?p=1#facturas"', ventana)


def test_un_borrador_del_proyecto_tiene_su_corregir_en_la_lista(con_proyecto, registro):
    """Sin un enlace, la ventana de corregir no se alcanza desde el proyecto."""
    f = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    bloque = _pagina(con_proyecto, p=1).split('id="facturas-del-proyecto"', 1)[1]
    assert f'href="/proyectos?p=1&amp;factura={f["id"]}&amp;corregir=1#facturas"' in bloque


def test_la_ventana_no_abre_una_factura_que_no_es_suya_ni_una_que_no_se_corrige(con_proyecto, registro):
    de_otro_proyecto = _factura_en_proyecto(con_proyecto, proyecto_id=2)
    emitida = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    _emitida_por_sql(con_proyecto, emitida["id"])
    for consulta in ({"p": 1, "factura": de_otro_proyecto["id"], "corregir": 1},   # de otro proyecto
                     {"p": 1, "factura": emitida["id"], "corregir": 1},            # ya emitida
                     {"p": 1, "factura": "no-es-un-numero", "corregir": 1},
                     {"p": 1, "factura": 99999, "corregir": 1}):                   # no existe
        html = _pagina(con_proyecto, **consulta)
        assert ABRE_LA_VENTANA not in html and "Corregir el borrador" not in html, consulta
        assert 'data-region="centro"' in html, consulta                            # la página sigue cargando


def test_un_borrador_abierto_sin_corregir_no_abre_nada(con_proyecto, registro):
    f = _factura_en_proyecto(con_proyecto, proyecto_id=1)
    html = _pagina(con_proyecto, p=1, factura=f["id"])
    assert ABRE_LA_VENTANA not in html


# ── 5. Solo ver no la ve ────────────────────────────────────────────────────────────────────────────

def test_solo_ver_no_abre_la_ventana_ni_carga_el_guion(con_proyecto, registro):
    html = _pagina(con_proyecto, con="ver", p=1, factura="nueva")
    assert ABRE_LA_VENTANA not in html and "<h2>Nueva factura</h2>" not in html
    assert 'id="guion-vista-previa"' not in html
    assert 'data-region="centro"' in html


# ── 6. El paso 2 se ve bien: la cascada que de verdad se aplica ─────────────────────────────────────

class _Nodo:
    def __init__(self, tag, attrs, padre):
        self.tag, self.attrs, self.padre = tag, attrs, padre
        self.hijos: list = []

    @property
    def clases(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def cadena(self) -> list:
        """De la raíz a este nodo (ambos incluidos)."""
        return ([*self.padre.cadena()] if self.padre else []) + [self]


class _Arbol(HTMLParser):
    VACIAS = {"input", "br", "img", "meta", "link", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = _Nodo("#raiz", {}, None)
        self.actual = self.raiz

    def handle_starttag(self, tag, attrs):
        n = _Nodo(tag, {k: (v or "") for k, v in attrs}, self.actual)
        self.actual.hijos.append(n)
        if tag not in self.VACIAS:
            self.actual = n

    def handle_endtag(self, tag):
        p = self.actual
        while p is not None and p.tag != tag:
            p = p.padre
        if p is not None and p.padre is not None:
            self.actual = p.padre


def _todos(n: _Nodo):
    yield n
    for h in n.hijos:
        yield from _todos(h)


_COMPUESTO = re.compile(r"([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[^\]]*\]|::?[a-z-]+(?:\([^)]*\))?)*)")


def _compuestos(selector: str) -> list[dict]:
    """`'.a .b'` → `[{'tag': None, 'clases': {'a'}, ...}, {...}]`. Solo descendencia (lo que usa esta hoja)."""
    selector = selector.strip()
    if not selector or ">" in selector or "+" in selector or "~" in selector:
        return []                      # combinadores que esta hoja no usa: que no cuenten (no se aplican)
    salida = []
    for parte in selector.split():
        m = _COMPUESTO.fullmatch(parte)
        if not m:
            return []
        tag, resto = m.group(1), m.group(2) or ""
        salida.append({"tag": tag,
                       "clases": set(re.findall(r"\.([\w-]+)", resto)),
                       "atributos": dict(re.findall(r"\[([\w-]+)(?:=[\"']?([^\"'\]]*)[\"']?)?\]", resto)),
                       "pseudos": re.findall(r"(?<!:):([a-z-]+)", resto)})
    return salida


def _coincide_compuesto(c: dict, n: _Nodo) -> bool:
    if c["tag"] and c["tag"] != n.tag:
        return False
    if not c["clases"] <= n.clases:
        return False
    for k, v in c["atributos"].items():
        if k not in n.attrs or (v and n.attrs[k] != v):
            return False
    for p in c["pseudos"]:
        if p == "last-child":
            if n.padre is None or n.padre.hijos[-1] is not n:
                return False
        elif p == "first-child":
            if n.padre is None or n.padre.hijos[0] is not n:
                return False
        else:
            return False               # un pseudo que no se sabe leer: la regla no cuenta
    return True


def _coincide(compuestos: list[dict], n: _Nodo) -> bool:
    if not compuestos or not _coincide_compuesto(compuestos[-1], n):
        return False
    antepasados = list(reversed(n.cadena()[:-1]))
    i = 0
    for c in reversed(compuestos[:-1]):
        while i < len(antepasados) and not _coincide_compuesto(c, antepasados[i]):
            i += 1
        if i == len(antepasados):
            return False
        i += 1
    return True


def _especificidad(compuestos: list[dict]) -> tuple:
    ids = clases = tags = 0
    for c in compuestos:
        ids += 0                                   # esta hoja no usa #id
        clases += len(c["clases"]) + len(c["atributos"]) + len(c["pseudos"])
        tags += 1 if c["tag"] else 0
    return (ids, clases, tags)


def _declaraciones(cuerpo: str) -> dict:
    salida = {}
    for trozo in cuerpo.split(";"):
        if ":" not in trozo:
            continue
        k, v = trozo.split(":", 1)
        salida[k.strip().lower()] = v.strip()
    return salida


def _reglas(css: str) -> list[tuple[list[dict], dict]]:
    """(selector ya partido, declaraciones) de cada regla, sin comentarios y sin los bloques `@…` (variables y modo
    oscuro: no son layout)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    reglas, i = [], 0
    while i < len(css):
        j = css.find("{", i)
        if j < 0:
            break
        cabecera = css[i:j].strip()
        if cabecera.startswith("@"):
            nivel, k = 1, j + 1
            while k < len(css) and nivel:
                nivel += {"{": 1, "}": -1}.get(css[k], 0)
                k += 1
            i = k
            continue
        k = css.find("}", j)
        if k < 0:
            break
        for selector in cabecera.split(","):
            compuestos = _compuestos(selector)
            if compuestos:
                reglas.append((compuestos, _declaraciones(css[j + 1:k])))
        i = k + 1
    return reglas


def _gana(reglas, nodo: _Nodo, propiedad: str) -> str | None:
    """El valor de `propiedad` que de verdad se aplica al nodo: la especificidad más alta y, a igualdad, la última."""
    mejor, valor = (-1, -1, -1), None
    for compuestos, declaraciones in reglas:
        if propiedad not in declaraciones or not _coincide(compuestos, nodo):
            continue
        clave = _especificidad(compuestos)
        if clave >= mejor:
            mejor, valor = clave, declaraciones[propiedad]
    return valor


def _flex(valor: str | None) -> tuple:
    """`'1 1 auto'` → `(1.0, 1.0, 'auto')`; `'1'` → `(1.0, 1.0, '0')` (el `flex:1` de siempre, base 0)."""
    if not valor:
        return (0.0, 1.0, "auto")
    partes = valor.split()
    if len(partes) == 1:
        if partes[0] in ("none", "initial"):
            return (0.0, 1.0, "auto")
        if partes[0] in ("auto",):
            return (1.0, 1.0, "auto")
        return (float(partes[0]), 1.0, "0")
    crecer = float(partes[0])
    encoger = float(partes[1]) if len(partes) > 1 else 1.0
    base = partes[2] if len(partes) > 2 else "0"
    return (crecer, encoger, base)


def test_el_paso_2_con_dos_sesiones_no_aplasta_el_texto(con_proyecto, registro):
    """El defecto que vio Tiziano: la casilla se quedaba con toda la fila (`.form-c input{width:100%}`, que es para las
    casillas de texto) y el texto de la sesión caía en una columna de una palabra por línea. Se mide sobre el CSS que la
    página sirve de verdad, con la cascada resuelta para la casilla y para el texto."""
    html = _pagina(con_proyecto, p=1, factura="nueva")
    arbol = _Arbol()
    arbol.feed(html)
    filas = [n for n in _todos(arbol.raiz) if n.tag == "label" and "sesion" in n.clases]
    assert len(filas) >= 2, f"el paso 2 tiene que traer al menos dos sesiones, trae {len(filas)}"
    fila = filas[0]
    casillas = [n for n in fila.hijos if n.tag == "input" and n.attrs.get("type") == "checkbox"]
    textos = [n for n in fila.hijos if n.tag == "span" and "que" in n.clases]
    assert len(casillas) == 1 and len(textos) == 1
    casilla, texto = casillas[0], textos[0]
    assert fila.hijos.index(casilla) < fila.hijos.index(texto), "la casilla va primero (a la izquierda)"

    css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    reglas = _reglas(css)
    assert reglas, "la página no trae CSS"

    # La casilla: su ancho, no el de la fila.
    assert _gana(reglas, casilla, "width") == "auto", \
        f"la casilla de la sesión se queda con el ancho de la fila: {_gana(reglas, casilla, 'width')}"
    assert _flex(_gana(reglas, casilla, "flex"))[0] == 0, "la casilla no puede crecer"

    # El texto: se queda con todo el ancho que sobra (base `auto`, no `0`), y puede partir una palabra larguísima.
    crecer, _, base = _flex(_gana(reglas, texto, "flex"))
    assert crecer >= 1 and base == "auto", \
        f"el texto de la sesión no crece (flex: {_gana(reglas, texto, 'flex')}): se queda en una columna"
    assert _gana(reglas, texto, "min-width") == "0"
    assert (_gana(reglas, texto, "overflow-wrap") or "") not in ("", "normal")

    # Y la fila es un renglón (casilla y texto lado a lado), no una tabla ni una reja.
    assert _gana(reglas, fila, "display") == "flex"


def test_la_hoja_de_la_factura_y_el_boton_de_continuar_traen_su_estilo(con_proyecto, registro):
    """Lo que vio la sala en Chrome (23-oct-2026): dentro de la página del proyecto, «Así va a salir» salía sin estilo
    («FACTURA DE SERVICIOS» en negro y enorme, PARA y FECHA pegados, la tabla sin cabecera roja ni la franja negra del
    total) y el botón de continuar, como el gris del navegador: `proyectos.html` no carga la hoja de las páginas de
    factura ni el `button{}` de `base.html`.

    Se derivan las clases de la HOJA QUE SE PINTA (el DOM servido, no una lista tecleada) y se exige que cada una tenga
    una regla en algún `<style>` de la página. Lo mismo con el botón: sus clases tienen que tener regla.
    """
    def pagina_y_reglas(**consulta):
        html = _pagina(con_proyecto, **consulta)
        arbol = _Arbol()
        arbol.feed(html)
        reglas = _reglas("".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)))
        return arbol, {c for compuestos, _ in reglas for comp in compuestos for c in comp["clases"]}

    # La hoja de «Así va a salir», en el paso 3.
    arbol, con_regla = pagina_y_reglas(p=1, factura="nueva", ref=["11", "12"], paso="escribir")
    caja = next((n for n in _todos(arbol.raiz) if "data-hoja" in n.attrs), None)
    assert caja is not None, "el paso 3 no trae la hoja de «Así va a salir»"
    clases_hoja = {c for n in _todos(caja) for c in n.clases}
    # Que la hoja esté dibujada de verdad: sin esto, lo de abajo sería verde por no mirar nada.
    assert {"hoja", "cod", "marca-f", "para-f", "sec-f", "tabla-f", "f-num", "tot-total", "pie"} <= clases_hoja, clases_hoja
    assert not (clases_hoja - con_regla), \
        f"la hoja de «Así va a salir» sale sin estilo: {sorted(clases_hoja - con_regla)} no tienen regla en ningún <style>"

    # El botón de continuar, en el paso 2.
    arbol, con_regla = pagina_y_reglas(p=1, factura="nueva")
    boton = next((n for n in _todos(arbol.raiz) if n.tag == "button" and n.attrs.get("value") == "escribir"), None)
    assert boton is not None, "el paso 2 no trae el botón de continuar"
    assert boton.attrs.get("name") == "paso"
    assert boton.clases, "el botón de continuar no lleva clase: en la página del proyecto sale como el del navegador"
    assert not (boton.clases - con_regla), f"el botón de continuar no tiene regla: {sorted(boton.clases - con_regla)}"


def test_ninguna_casilla_de_marcar_de_la_ventana_se_queda_con_la_fila(con_proyecto, registro):
    """El paso 3 tiene su propia casilla (el ITBIS) y también caía en `.form-c input{width:100%}`."""
    html = _pagina(con_proyecto, p=1, factura="nueva", ref=["11"], paso="escribir")
    arbol = _Arbol()
    arbol.feed(html)
    casillas = [n for n in _todos(arbol.raiz) if n.tag == "input" and n.attrs.get("name") == "itbis"]
    assert casillas, "el paso 3 no trae la casilla del ITBIS"
    reglas = _reglas("".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)))
    for casilla in casillas:
        assert _gana(reglas, casilla, "width") == "auto"


def test_la_cascada_que_mide_el_paso_2_ve_el_defecto(con_proyecto, registro):
    """Que el verde de arriba signifique algo: con las dos reglas del defecto puestas A MANO (la casilla al 100% y el
    texto con base 0), la misma medición las ve."""
    html = _pagina(con_proyecto, p=1, factura="nueva")
    arbol = _Arbol()
    arbol.feed(html)
    fila = next(n for n in _todos(arbol.raiz) if n.tag == "label" and "sesion" in n.clases)
    casilla = next(n for n in fila.hijos if n.tag == "input")
    texto = next(n for n in fila.hijos if n.tag == "span" and "que" in n.clases)
    reglas = _reglas(".form-c input{width:100%}.sesion .que{flex:1}")
    assert _gana(reglas, casilla, "width") == "100%"
    assert _flex(_gana(reglas, texto, "flex"))[2] == "0"
