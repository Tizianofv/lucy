"""El correo y el RNC o cédula de la formal (24-oct-2026; Tiziano: «Debe llevar Nombre, correo, teléfono y RNC o cédula si
es con comprobante»). Qué se vigila:

  · la base: el correo y el RNC se guardan, el RNC sale solo con cifras (la puerta de la factura, `rnc_o_cedula_que_vale`),
    lo vacío queda NULL, un valor que no vale no escribe nada y editar sin tocarlos los conserva;
  · la API de Natalia: los acepta con la misma validación que la casa (422 si no valen);
  · la hoja (vista previa y PDF, que dibujan la misma lista): salen solo si vienen, en el orden del bloque «Para»;
  · la ventana: en `test_cotizaciones_correo_rnc_ventana.py`.

Lo que NO está aquí: que la formal sin esos datos salga igual que antes (eso se midió contra `git archive 83b1bdf` con
un volcado de la hoja, fuera de la suite).
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from test_guardar_cotizacion import con, _guardar, _fila, _cuantas  # noqa: F401  (el fixture `con` vive allá)
from test_cotizaciones_natalia_puerta import mundo, llamar, _fila as _fila_sql, _cuenta, CONTENIDO_BASE  # noqa: F401

import cotizaciones  # primero las pruebas: fijan el entorno antes de cargar `config`
import db.db as db


def _cot_hoja(**extra):
    base = {"codigo": "COT-2026-001", "para_nombre": "Ana Pérez", "para_telefono": None, "fecha": None,
            "validez_dias": 15,
            "contenido": {"opciones": [{"nombre": "Básica", "renglones": [
                {"descripcion": "Mezcla", "detalle": "", "total": "18000"}]}],
                "condiciones": [{"clave": None, "texto": "Los precios no incluyen impuestos.", "marcada": True}]}}
    base.update(extra)
    return base


# ── La base ──────────────────────────────────────────────────────────────────────────────────────

def test_el_correo_y_el_rnc_se_guardan_y_el_rnc_sale_solo_con_cifras(con):
    r = _guardar(para_correo="  ana@ejemplo.do ", para_rnc_cedula="131-23456-7")
    assert (r["para_correo"], r["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567")
    fila = _fila(con)
    assert (fila["para_correo"], fila["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567")


def test_cedula_de_once_cifras_tambien_vale(con):
    assert _guardar(para_rnc_cedula="001 1234567 8")["para_rnc_cedula"] == "00112345678"


def test_sin_correo_ni_rnc_quedan_nulos_y_lo_vacio_tambien(con):
    r = _guardar()
    assert (r["para_correo"], r["para_rnc_cedula"]) == (None, None)
    r = _guardar(para_correo="", para_rnc_cedula="   ")
    assert (r["para_correo"], r["para_rnc_cedula"]) == (None, None)


@pytest.mark.parametrize("malo, clave", [
    ("123", "para_rnc_cedula_formato"),
    ("12345678a", "para_rnc_cedula_formato"),
    ("1234567890", "para_rnc_cedula_formato"),
    (123456789, "para_rnc_cedula_tipo"),
])
def test_un_rnc_que_no_vale_no_escribe_nada(con, malo, clave):
    with pytest.raises(db.CotizacionNoVale) as e:
        _guardar(para_rnc_cedula=malo)
    assert e.value.clave == clave
    assert _cuantas(con) == 0


def test_un_correo_de_mas_de_254_caracteres_no_se_guarda(con):
    with pytest.raises(db.CotizacionNoVale) as e:
        _guardar(para_correo="a" * 250 + "@x.do")
    assert e.value.clave == "para_correo_largo" and _cuantas(con) == 0
    assert _guardar(para_correo="a" * 248 + "@x.do")["para_correo"] == "a" * 248 + "@x.do"


def test_editar_sin_tocar_conserva_y_con_vacio_borra(con):
    _guardar(para_correo="ana@ejemplo.do", para_rnc_cedula="131234567")
    candado = _fila(con)["cambiado_en"]
    r = _guardar("borrador", cotizacion_id=1, cambiado_en=candado, para_nombre="Ana")
    assert (r["para_correo"], r["para_rnc_cedula"]) == ("ana@ejemplo.do", "131234567")
    candado = _fila(con)["cambiado_en"]
    r = _guardar("borrador", cotizacion_id=1, cambiado_en=candado, para_correo="", para_rnc_cedula="")
    assert (r["para_correo"], r["para_rnc_cedula"]) == (None, None)


# ── La API de Natalia ────────────────────────────────────────────────────────────────────────────

def test_la_api_de_natalia_acepta_correo_y_rnc_y_los_guarda(mundo):
    r = llamar("POST", "/api/code/clientes/7/cotizaciones",
               json={"contenido": CONTENIDO_BASE, "validez_dias": 15,
                     "para_correo": "cliente@ejemplo.do", "para_rnc_cedula": "131-23456-7"})
    assert r.status_code == 200, r.text
    fila = _fila_sql(mundo, "SELECT para_correo, para_rnc_cedula FROM cotizaciones WHERE id = ?",
                     (r.json()["cotizacion"]["cotizacion_id"],))
    assert fila == {"para_correo": "cliente@ejemplo.do", "para_rnc_cedula": "131234567"}


def test_la_api_de_natalia_valida_el_rnc_como_la_casa_y_no_escribe(mundo):
    r = llamar("POST", "/api/code/clientes/7/cotizaciones",
               json={"contenido": CONTENIDO_BASE, "validez_dias": 15, "para_rnc_cedula": "123"})
    assert r.status_code == 422, r.text
    assert _cuenta(mundo, "cotizaciones") == 0


def test_sin_los_campos_nuevos_la_api_de_natalia_sigue_igual(mundo):
    r = llamar("POST", "/api/code/clientes/7/cotizaciones", json={"contenido": CONTENIDO_BASE, "validez_dias": 15})
    assert r.status_code == 200, r.text
    fila = _fila_sql(mundo, "SELECT para_correo, para_rnc_cedula FROM cotizaciones WHERE id = ?",
                     (r.json()["cotizacion"]["cotizacion_id"],))
    assert fila == {"para_correo": None, "para_rnc_cedula": None}


# ── La hoja (vista previa y PDF) ─────────────────────────────────────────────────────────────────

def test_la_hoja_pone_correo_y_rnc_en_el_bloque_para_solo_si_vienen():
    con_todo = cotizaciones.textos_de_la_hoja(cotizaciones.hoja_de_cotizacion(
        _cot_hoja(para_correo="ana@ejemplo.do", para_telefono="809-555-0101", para_rnc_cedula="131234567",
                  fecha=date(2026, 10, 14))))
    assert con_todo[2:14] == ["PARA", "Ana Pérez", "CORREO", "ana@ejemplo.do", "TELÉFONO", "809-555-0101",
                              "RNC O CÉDULA", "131234567", "FECHA", "14 de octubre de 2026", "VALIDEZ", "15 días"]
    sin = cotizaciones.textos_de_la_hoja(cotizaciones.hoja_de_cotizacion(
        _cot_hoja(para_correo=None, para_rnc_cedula=None, fecha=date(2026, 10, 14))))
    assert "CORREO" not in sin and "RNC O CÉDULA" not in sin
    assert sin[2:6] == ["PARA", "Ana Pérez", "FECHA", "14 de octubre de 2026"]


def test_el_check_de_la_base_frena_el_correo_largo_y_el_rnc_de_mas(con):
    _guardar()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE cotizaciones SET para_correo = ? WHERE id = 1", ("a" * 255,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE cotizaciones SET para_rnc_cedula = ? WHERE id = 1", ("1" * 21,))
    con.execute("UPDATE cotizaciones SET para_correo = ?, para_rnc_cedula = ? WHERE id = 1", ("a" * 254, "1" * 11))
