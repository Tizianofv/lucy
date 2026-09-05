# Diseño: cargar un gasto en efectivo desde el panel

> Escrito el 4-sep-2026 contra el commit `6f7d4da`, que es el que corre en
> producción (Railway, proyecto `giving-balance`, servicio `lucy`, despliegue
> `4053a10e`, SUCCESS). Las consultas de más abajo se corrieron contra la base de
> producción.
>
> **Esto es un diseño para aprobar. No se escribió ni una línea de código.**

---

## Lo que pediste

> "Quiero agregar, en el panel de gastos que Lucy tiene, una forma de agregar un
> gasto en efectivo, algo manual, que yo pueda escribir el concepto y poner el
> monto, seleccionar la categoría y que sea en efectivo."

Cuatro cosas: **concepto, monto, categoría, y que quede marcado como efectivo.**

---

## La respuesta corta

**No hace falta una columna nueva, ni tocar el esquema, ni un respaldo, ni una
migración.**

Se agrega un formulario plegado arriba de la tabla de `/movimientos` y una ruta
`POST /efectivo`. La fila que escribe es igual a cualquier otra, con una sola
diferencia: en la columna `banco` dice `efectivo` en vez de `bhd`.

Con eso, "¿cuánto gasté en efectivo?" se contesta **hoy, sin código nuevo**:
`/movimientos?banco=efectivo`. El desplegable de bancos de esa pantalla se arma
solo desde lo que hay en la base, así que la opción "efectivo" aparece sola en
cuanto exista la primera fila.

Lo único que cuesta es que una columna que se llama `banco` va a tener un valor
que no es un banco. Es el precio, y abajo digo por qué me parece barato.

---

## Por qué no hace falta columna nueva

Primero comprobé que el método de pago de verdad no existe. La base de producción
tiene 17 columnas en `movimientos` y ninguna dice cómo se pagó:

```
id · bandeja_id · creado_en · fecha · monto · moneda · contraparte · categoria
notas · borrado_en · tipo · referencia · persona_id · proyecto_id
hash_contenido · banco · estado
```

Así que la pregunta es: ¿alguna de las que ya están sirve, sin mentir?

**`banco` sirve.** Es la columna que dice *de dónde salió la plata*. Hoy tiene
cinco valores, todos en minúscula, y las 129 filas vivas tienen uno puesto:

```
apap 5 · banesco 44 · banreservas 12 · bhd 65 · popular 3
```

"Efectivo" es una respuesta legítima a "de dónde salió la plata". La columna se
llama `banco` porque hasta hoy la única fuente eran correos de bancos, no porque
el concepto sea "banco".

**Y lo importante: `banco` NO es lo que el código usa para saber si una fila vino
del correo.** Eso lo decide `hash_contenido`. Lo comprobé en las tres consultas
donde importaría:

* `db/db.py:1143` — `silencio_por_banco()`, la tabla "Por banco" de `/salud`,
  filtra `hash_contenido IS NOT NULL`. Un gasto en efectivo **no** va a aparecer
  ahí como un banco del que hace 12 días que no entra nada.
* `db/db.py:1116` — `salud_ingesta()`, el contador de "automáticos", filtra lo
  mismo. No se infla.
* `db/db.py:986` — `posibles_duplicados()` trabaja sobre `hash_contenido`. Un
  gasto en efectivo no puede aparecer como duplicado sospechoso.

O sea: la maquinaria de salud ya está protegida, y no por casualidad — está
escrito en el docstring de `silencio_por_banco`: *"un movimiento cargado a mano no
dice nada sobre si la ingesta de ese banco funciona"*.

**Lo que sí gana, gratis:**

* `db/db.py:1060` — `bancos_usados()` arma el desplegable de la pantalla. Suma
  "efectivo" solo.
* `db/db.py:1086` — `movimientos_filtrados()` ya filtra por `banco` exacto. El
  filtro funciona sin tocar nada.
* `web/plantillas/movimientos.html:96` — la columna "Banco" ya se pinta. Va a
  decir "efectivo".

### Y por qué una columna nueva sería peor

Si mañana agregáramos `metodo_pago`, habría que contestar qué ponerle a las 129
filas que ya están. Y **no se puede contestar bien**: esas filas incluyen compras
con tarjeta, transferencias, nómina, pagos de servicio e intereses, y el dato
fino que los separa (`canal`, en `cerebro/bancos/contrato.py:51`) **nunca se
guardó** — el contrato dice que "viaja en `referencia`", que es texto de
presentación.

Resultado: la columna nueva quedaría con 129 nulos. Y una columna donde
`efectivo` significa "efectivo" pero `NULL` significa "ni idea" contesta
"¿cuánto gasté en efectivo?" y no contesta "¿cuánto gasté con tarjeta?". Media
columna, que además hay que mantener.

Con `banco` el problema no existe: **las 129 filas ya tienen su valor puesto** y
la columna sigue completa.

Y hay un costo de despliegue que también desaparece: el arranque de Lucy compara
qué **tablas** faltan (`db/db.py:36-63`), pero **no mira columnas**. Con una
columna nueva, si el código llega antes que el `ALTER TABLE`, Lucy arranca
callada y el panel tira Internal Server Error hasta que alguien corra la
migración. Sin columna nueva, no hay hueco que cuidar.

---

## La pantalla

**Dónde:** en `/movimientos`, arriba de la tabla, dentro de un `<details>`
cerrado que dice **"+ Gasto en efectivo"**. Se abre con un toque y no empuja la
tabla hacia abajo cuando no se usa.

**Por qué ahí y no en `/sin-clasificar`:** esa pantalla es la cola de corrección
y tiene un test que exige **un solo `<form>`** en la plantilla
(`tests/test_panel.py:151`) — un segundo formulario ahí rompe la razón por la que
ese test existe. `/movimientos` es el registro: es donde vas a mirar si quedó, y
donde vive el filtro por banco que hace útil la marca.

**Por qué no una pantalla nueva:** una pantalla nueva es una entrada más en el
menú y una ruta más que proteger, para un formulario de tres campos.

**El formulario, tres campos y un botón:**

```
Concepto     [ texto                          ]   ej. "parqueo en la Zona Colonial"
Monto        [ 0.00          ] RD$
Categoría    [ desplegable ▾ ]                    las 26 de siempre, en alfabético
                                                  + "— sin categoría —"
                         [ Agregar en efectivo ]
```

Debajo, en gris: *"Se guarda con la fecha de hoy, en pesos, marcado como
efectivo."* — para que no haya que adivinar qué hizo.

**No lleva ni banco ni método de pago**, porque el formulario ya *es* el de
efectivo: elegir "efectivo" en un formulario que solo carga efectivo es una
decisión que no existe. (Es la regla de siempre: antes de manejar el caso,
borrarlo.)

Va como formulario **hermano** del que ya está, nunca adentro: la tabla vive
dentro de `<form action="/categorias">` y un `<form>` dentro de otro es HTML
inválido, que es lo mismo que ya obligó a usar `formaction` en el botón de la
papelera (`movimientos.html:123`).

---

## La ruta

**`POST /efectivo`** → redirige `303` de vuelta a `/movimientos` con los filtros
que estaban puestos, igual que hacen `/categorias` y `/borrar`.

**La puerta:** primera línea del endpoint,
`if not auth.puede_entrar(_sesion(request)): return _fuera(request)` — idéntica a
las otras ocho rutas. Con eso pasa `test_todas_las_rutas_estan_protegidas`
(`tests/test_panel.py:100`), que recorre `app.routes` y lee el código fuente
buscando `puede_entrar`.

**El destino del redirect** sale de `_destino_seguro()`, la función que ya existe
y ya está probada contra `//evil.com` y compañía. No se escribe una segunda.

**Qué valida, en este orden:**

| Campo | Regla | Si falla |
|---|---|---|
| concepto | `.strip()` no vacío, máximo 200 caracteres | vuelve con aviso, no guarda |
| monto | casa `^\d{1,10}(\.\d{1,2})?$`, y `Decimal(...) > 0` | vuelve con aviso, no guarda |
| categoría | vacía, o en `CATEGORIAS` **y** `categoria_permitida("gasto", c)` | vuelve con aviso, no guarda |

Tres cosas que valen la pena decir:

* **El monto se rechaza, no se redondea.** `1.234` no se guarda como `1.23`: se
  rechaza. La columna es `NUMERIC(12,2)` y Postgres redondearía en silencio, que
  es exactamente lo que este proyecto no hace con dinero. Sin separadores de
  miles: el campo es `type="number"`.
* **La categoría se valida contra la lista cerrada aunque el desplegable ya solo
  ofrezca esas.** Mismo motivo que en `/categorias`: un POST a mano metería
  "supermercado" en minúscula y partiría el total en dos para siempre.
* **Basura no revienta.** Cualquier rechazo es un `303` de vuelta con
  `?error=...`, nunca un 500. Y el rechazo queda en el log del servidor, como
  hace `/categorias` con las categorías rechazadas.

**No lleva guarda de duplicados.** El alta por Telegram sí la tiene
(`acciones/crud.py:318-336`), porque ahí el riesgo es chocar con un movimiento
que el correo del banco ya trajo. En efectivo eso no puede pasar: **ningún banco
manda un correo por un pago en efectivo.** Y dos cafés de RD$150 el mismo día son
normales. El `303` ya evita el duplicado por refrescar la página, y si igual se
cuela uno, el 🗑 lo manda a la papelera.

---

## La fila, columna por columna

| Columna | Qué se escribe | Por qué |
|---|---|---|
| `bandeja_id` | `NULL` | No hay mensaje de Telegram ni correo detrás. La FK acepta nulo. |
| `creado_en` | *default* `now()` | Cuándo se cargó, no cuándo se gastó. |
| `tipo` | `'gasto'` | Fijo. El formulario es de gastos. |
| `fecha` | hoy en Santo Domingo — `datetime.now(config.TZ).date()` | Es `NOT NULL` y no pediste campo de fecha. Ver la última sección. |
| `monto` | el `Decimal` validado, pasado como `str` | Como en `db/db.py:738`: por `float` se pierden centavos. |
| `moneda` | `'DOP'` | Fijo. El efectivo de esta casa es en pesos. |
| `contraparte` | el concepto, `.strip()` | Es la columna que el panel muestra bajo "Quién" y es donde el alta por Telegram ya pone el concepto. |
| `categoria` | la elegida, o `NULL` | Si va vacía, la fila aparece sola en `/sin-clasificar`, que es lo que ya hace todo lo demás. |
| `referencia` | `NULL` | Es "No. de confirmación / comprobante". El efectivo no tiene. **No** se escribe "efectivo" ahí: es texto de presentación y sería un dato escondido en un campo libre. |
| `persona_id`, `proyecto_id`, `notas` | `NULL` | No los pediste. |
| `hash_contenido` | `NULL` | **La más importante.** Es lo que mantiene la fila fuera de los contadores de ingesta de `/salud` y fuera de "posibles duplicados". Además el índice único es parcial (`WHERE hash_contenido IS NOT NULL`), así que muchos nulos conviven sin chocar. |
| `banco` | `'efectivo'` | En minúscula, para casar con `bhd`, `apap`, `banesco`, `banreservas`, `popular`. |
| `estado` | `'aprobada'` (el default) | Pasa el `CHECK movimientos_estado_valido`, que solo admite `aprobada / declinada / pendiente`. |
| `borrado_en` | `NULL` | Fila viva. |

Es el mismo molde del alta por Telegram (`acciones/crud.py:340-352`), con `banco`
puesto en vez de dejado en nulo.

**Una cosa que conviene saber:** hoy la pantalla explica que las filas con "—" en
Banco *"se anotaron a mano por Telegram antes de que existiera la lectura de
correos"* (`movimientos.html:136-140`). Ese texto sigue siendo verdad y no hay que
tocarlo: los gastos en efectivo van a decir "efectivo", no "—".

---

## El deshacer

La fila y su huella se escriben **en el mismo bloque de conexión**, o sea en la
misma transacción, igual que `a_la_papelera()` (`db/db.py:893-912`). O entran las
dos o no entra ninguna.

```
log_acciones
  actor        'panel'
  accion       'crear'
  tabla        'movimientos'
  registro_id  <el id nuevo>
  antes        NULL          ← no había nada antes
  despues      {la fila entera, en JSON}
  motivo       'gasto en efectivo cargado desde el panel'
```

Con eso hay **dos formas de deshacerlo, las dos ya construidas**:

1. El botón 🗑 de la fila, en la misma pantalla. Va a la papelera, se recupera 30 días.
2. `deshacer(log_id)` por Telegram: su rama de `'crear'` (`acciones/crud.py:797-801`)
   hace `SET borrado_en = now()`, o sea la manda a la papelera también.

No hace falta inventar nada. Solo hay que escribir `accion='crear'` y no otra
palabra, porque es lo que `deshacer` sabe revertir.

---

## Cómo se despliega

**Sin migración, sin DDL, sin respaldo previo, sin hueco.** Es todo el punto de la
recomendación: el código nuevo no nombra ninguna columna que no exista ya en
producción, así que llega solo y funciona desde el primer segundo.

El orden queda así:

1. Se escribe el código y se corren las 21 suites en local.
2. Se corre `python3 tools/humo.py` con `DATABASE_URL`. Hoy solo prueba lecturas;
   habría que sumarle un caso: `movimientos_filtrados(banco="efectivo")`. Es una
   lectura, no escribe nada.
3. Se publica.
4. **La prueba de verdad, después de publicar:** cargar un gasto en efectivo real
   desde el celular, comprobar que aparece en `/movimientos?banco=efectivo`, que
   suma en la portada del mes, y mandarlo a la papelera si era de prueba. Es la
   única forma de saber que el INSERT corre contra la tabla real; ninguna suite
   puede decirlo (abajo se explica por qué).

*(Si en vez de esto elegís la columna nueva, entonces sí: respaldo con
`python3 db/backup.py` primero, `ALTER TABLE` después, y recién ahí el código —
porque el arranque de Lucy no comprueba columnas y no avisaría.)*

---

## Las pruebas nuevas

**Las que la suite actual sí puede verificar** (van en `tests/test_panel.py`):

1. `test_la_ruta_de_efectivo_exige_sesion` — llamar `POST /efectivo` sin cookie y
   exigir 401. *(Además, `test_todas_las_rutas_estan_protegidas` la agarra sola en
   cuanto exista: recorre `app.routes`.)*
2. `test_el_formulario_de_efectivo_no_esta_anidado` — leer `movimientos.html` y
   exigir que el `<form>` nuevo **cierre antes** de que abra
   `<form action="/categorias">`. Un formulario anidado lo descarta el navegador y
   el botón dejaría de hacer nada, en silencio.
3. `test_efectivo_rechaza_montos_que_no_son_numeros` — con
   `"", "abc", "-5", "0", "0.00", "1.234", "1e5", "1,500", " ", "9"*20` exigir que
   no se llame al insert y que la respuesta sea `303`, no un 500.
4. `test_efectivo_rechaza_categorias_de_fuera_del_vocabulario` — con
   `"supermercado"`, `"Salario"`, `"<script>"` exigir que no se guarde.
5. `test_efectivo_rechaza_un_concepto_vacio` — `""`, `"   "`, y 201 caracteres.
6. `test_el_gasto_en_efectivo_se_guarda_con_los_campos_que_toca` — espiar la
   función de `db` y exigir `banco="efectivo"`, `tipo="gasto"`, `moneda="DOP"`,
   `hash_contenido` nulo, `bandeja_id` nulo, `monto` `Decimal`.
7. `test_el_redirect_del_efectivo_no_acepta_destinos_de_afuera` — los mismos
   destinos hostiles que ya prueba `_destino_seguro`, pero entrando por esta ruta.
8. Ampliar `test_las_pantallas_se_pintan_de_verdad` para exigir que el HTML de
   `/movimientos` traiga `action="/efectivo"`. Esa prueba **renderiza de verdad**,
   así que si el formulario usa una variable que la ruta no manda, se cae ahí.

**Las que la suite NO puede verificar, y hay que decirlo.**
`tests/test_panel.py:23-31` reemplaza `psycopg` y `psycopg_pool` por módulos
falsos antes de importar nada. Nunca habla con Postgres. Por construcción **no
puede ver**:

* que el `INSERT` case con la tabla real (nombres de columna, tipos);
* que `estado='aprobada'` pase el `CHECK`;
* que la fila aparezca de verdad en la portada, en la cola y en el filtro;
* que `bancos_usados()` empiece a devolver `"efectivo"`;
* que la fila de `log_acciones` se escriba y que `deshacer` la revierta.

Eso lo cubre el paso 4 de arriba —cargar un gasto real y mirarlo—, más el caso
nuevo de `tools/humo.py`. Un verde en las 21 suites **no dice nada** sobre esto.
Es exactamente la familia de fallos por la que existe `humo.py`.

---

## Esto lo decidís vos

**1. La fecha.** Tal como está diseñado, el gasto se guarda **con la fecha de hoy**
y no hay campo de fecha — porque no lo pediste y un campo menos es un campo menos.
El costo: si el sábado gastaste RD$500 en efectivo y lo cargás el lunes, queda
anotado el lunes, y el resumen por mes lo cuenta en el mes del lunes si el sábado
fue fin de mes. ¿Lo dejamos así, o le agrego un campo de fecha que viene con hoy
puesto y se puede cambiar?

**2. Preguntar por tarjeta, transferencia o nómina.** Este diseño te deja
preguntar "¿cuánto gasté en efectivo?" y nada más. Si algún día querés preguntar
"¿cuánto gasté con tarjeta?" o "¿cuánto salió por transferencia?", **eso sí pide
una columna nueva**, y las 129 filas que ya están no se pueden rellenar bien
porque ese dato nunca se guardó. Sería un encargo aparte y más caro. ¿Lo querés en
la cola, o no hace falta?
