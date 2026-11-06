# Persistencia, sesiones y cuotas de Muvio {#muvio-persistence}

Muvio permite elegir retención persistente o temporal y aísla los trabajos por sesión anónima. **Las cuentas y la publicación pública siguen pendientes**; la transcripción requiere administración.

- `PERSIST_STORAGE` es global: también se aplica a administración y solo cambia los trabajos nuevos.
- Los temporales caducan desde la finalización; configuración, modelos, firma y cuota permanecen fuera de los medios.
- La cuota es transaccional y sobrevive al reinicio. `ANON_DOWNLOAD_LIMIT=0` significa ilimitado, no deshabilitado.
- La entrega cubre almacenamiento, aislamiento anónimo, admisión y experiencia de usuario; no promete capacidad distribuida.

@answer scope="Instancia única · sin cuentas · valores iniciales, no capacidad medida"
@meta locale=es style=clean quality_profile=showcase

## ¿Dónde se configura cada cosa? {#configuration}

| Ubicación | Qué configurar | Qué no hacer |
| --- | --- | --- |
| `compose.yml` | Rutas, persistencia, TTL, cuota, cookies, origen y límites de recursos; incluye valores comentados | No guardar secretos |
| `.env` | Solo los secretos de `.env.example` | No versionar el archivo ni guardar claves en Compose |
| Settings | Preferencias administrativas de modelos y LLM | No modifica cuota, retención ni controles de acceso |
| `config.json` | Preferencias guardadas desde Settings | Conserva precedencia sobre sus valores iniciales de entorno |

Después de cambiar Compose, aplica la configuración con este comando:

```bash title="Aplicar los cambios de Compose"
docker compose up -d --build
```

@callout level=warning title="Guarda tus temporales antes de recrear el contenedor"
| El comando puede recrearlo y perder los temporales. Descarga antes los resultados necesarios. |
| La implementación no ha ejecutado ese despliegue ni modificado el contenido existente de `data/`. |

## ¿Qué queda en el volumen y qué es temporal? {#storage}

@architecture id=storage-map title="Raíces y retención global"
| visitor | Sesión privada | frontend |
| muvio | Muvio | backend |
| state | Estado estable | database |
| persistent | Medios persistentes | database |
| temporary | Medios temporales | database |
| models | Modelos | database |
| visitor > muvio | Propietario | security |
| muvio > state | Siempre |
| muvio > persistent | true | emphasis |
| muvio > temporary | false | emphasis |
| muvio > models | Siempre |

Mapa lógico de una sola instancia: las cajas no son réplicas ni servicios independientes. `true` y `false` representan `PERSIST_STORAGE`. No existe una excepción que haga siempre temporales los trabajos anónimos.

| Datos | Ubicación | Retención y privacidad |
| --- | --- | --- |
| Resultados persistentes | `OUTPUT_DIR/<id>/` | Sin caducidad automática; el limpiador no los borra para hacer sitio |
| Medios temporales | `TEMP_STORAGE_DIR/jobs/<id>/` | Disco ordinario fuera del volumen, no RAM ni `tmpfs` |
| Historial temporal | `TEMP_STORAGE_DIR/history.db` | No se copia al estado persistente; se purga tras un plazo acotado |
| Configuración, cuota y firma | `STATE_DIR` | Persisten en ambos modos; no incluyen historial de medios temporales |
| Historial persistente | `STATE_DIR/history.db` | Solo historial persistente y legado; acceso por propietario |
| Modelos de inferencia | `MODELS_DIR` | Se comparten los modelos, nunca una caché de medios entre visitantes |

Subidas, `.part`, audio preparado, portadas, subtítulos y separación de voces permanecen en el espacio privado del trabajo. Al terminar se conservan solo las exportaciones descargables. El parser multipart usa temporales sin nombre en `TEMP_STORAGE_DIR/incoming`, con cuerpo acotado y cierre al salir. Esta raíz no se monta como volumen ni como `tmpfs`.

## ¿Cuáles son los valores iniciales? {#defaults}

| Variable | Inicial | Significado |
| --- | --- | --- |
| `PERSIST_STORAGE` | `true` | Retención de trabajos nuevos, no de trabajos históricos |
| `OUTPUT_DIR` | `/app/data/downloads` | Resultados persistentes |
| `STATE_DIR` | `/app/data` en Compose | Configuración, firma, historial persistente, cuota y bloqueo de instancia |
| `MODELS_DIR` | `/app/data/models` | Modelos compartidos, sin caché de medios entre visitantes |
| `TEMP_STORAGE_DIR` | `/tmp/muvio` | Medios e historial temporal fuera del volumen |
| `TEMP_FILE_TTL_SECONDS` | `3600` | Desde la finalización correcta, no desde la admisión |
| `TEMP_CLEANUP_INTERVAL_SECONDS` | `60` | Limpieza al arrancar y periódica |
| `TEMP_HISTORY_TTL_SECONDS` | `86400` | Conservación adicional del historial temporal tras caducidad o fallo |
| `ANON_DOWNLOAD_LIMIT` | `5` | Cero significa ilimitado; negativo es inválido |
| `ANON_DOWNLOAD_WINDOW_SECONDS` | `86400` | Ventana móvil real por sesión |
| `SESSION_MAX_AGE_SECONDS` | `2592000` | Cookie renovable; debe cubrir ventana de cuota más TTL |
| `COOKIE_SECURE` | `false` | Solo para localhost HTTP; activar con HTTPS |
| `PUBLIC_ORIGIN` | Vacío | Fijar el origen exacto del navegador con proxy, como `https://muvio.example` |
| `API_KEY_REQUIRED` | `false` | Puerta opcional para toda la instalación; administración siempre requiere clave |

El grupo «Conservative starting controls» de Compose reúne trabajadores, admisiones globales y por propietario, inspecciones simultáneas y almacenamiento por trabajo. También controla margen libre de disco, duración de fuente, procesamiento, subidas, transferencias y SSE.

La admisión reserva presupuesto de disco por cada trabajo activo. Si se alcanza el presupuesto persistente, se rechazan nuevos trabajos; **no se borran resultados persistentes para hacer sitio**. Estos controles son configurables y no sustituyen una prueba de capacidad del servidor.

## ¿Quién puede ver y administrar un trabajo? {#identity}

La sesión usa una cookie de alta entropía, firmada con un secreto estable, `HttpOnly` y `SameSite=Lax`. El servidor deriva un propietario opaco: no acepta propietarios enviados por el cliente ni usa la IP como identidad.

| Mecanismo | Qué permite | Qué no implica |
| --- | --- | --- |
| Sesión anónima | Consultar y operar sus propios trabajos | No es una cuenta ni una identidad por persona |
| Clave de instalación | Concede rol administrativo a la sesión actual; gestiona preferencias, modelos y legado | No crea una identidad compartida ni permite ver trabajos de otros visitantes |
| Autorización en servidor | Filtra listados, detalles, cancelaciones y emisión de enlaces por propietario | Un identificador ajeno responde igual que uno inexistente |
| Historial y SSE | Paginación por cursor; solo trabajos activos en memoria; eventos filtrados y buffers limitados | No publica un historial ni eventos globales |

La cuota guarda identificadores, hashes y marcas de tiempo; sus registros se purgan tras la ventana. Borrar cookies o cambiar de navegador permite eludir una cuota anónima. No se ofrece una garantía por persona.

No compartas la clave administrativa para simular cuentas. Para salir de administración, vacía la clave y pulsa «Use key / leave administration». La UI renueva los flujos de eventos y sus cachés.

## ¿Cuándo se reserva, cobra o devuelve una cuota? {#quota}

@workflow id=admission-flow title="Admisión y consumo por trabajo"
| lane admission | Admisión |
| lane processing | Procesamiento |
| lane outcome | Contabilidad |
| identify | Identificar sesión | security | admission |
| reserve | Reservar cupo | database | admission |
| valid | ¿Validación correcta? | backend | admission |
| enqueue | Encolar trabajo | messagebus | processing |
| process | Iniciar proceso | backend | processing |
| release | Reserva liberada | database | outcome |
| charged | Consumo conservado | database | outcome |
| identify > reserve | |
| reserve > valid | |
| valid > enqueue | Sí | emphasis |
| valid > release | No |
| enqueue > release | Cancelación previa |
| enqueue > process | Inicio | emphasis |
| process > charged | Fin o fallo | emphasis |

La reserva SQLite es atómica e incluye inspecciones y admisiones concurrentes. Sobrevive al reinicio; las reservas interrumpidas se reconcilian al arrancar. Cada elemento solicitado de una colección consume su propio cupo.

| Situación | Contabilidad |
| --- | --- |
| Selector de colección sin iniciar un trabajo | Libera la reserva |
| Validación rechazada o fallo previo a admisión | Libera la reserva |
| Cancelación antes del comienzo | Libera el cupo |
| Error o cancelación después del comienzo | Cuenta, porque ya consumió recursos |
| `ANON_DOWNLOAD_LIMIT=0` | `remaining=null`; siguen los controles de cola, recursos, inspección y tráfico por IP |
| `429 / quota.exceeded` | Incluye límite, restantes, próxima disponibilidad en segundos Unix y `Retry-After` |

@callout level=warning title="Si falla el estado, no se convierte en ilimitado"
| Un fallo de persistencia durante admisión o finalización bloquea nuevas admisiones. Restaura el almacenamiento y reinicia el servidor para reconciliarlo; no borres la base de cuotas. |
| Un error de contabilidad no debe matar silenciosamente un worker ni ofrecer capacidad ilimitada. |

## ¿Cómo se evitan los trabajos duplicados? {#idempotency}

`Idempotency-Key` queda vinculado a la sesión y a la huella de la petición durante la ventana. La UI conserva la clave de un intento sin respuesta para poder repetirlo.

| Reintento | Respuesta |
| --- | --- |
| Misma clave y petición ya admitida | Devuelve el mismo trabajo, sin duplicarlo ni cobrarlo otra vez |
| Inspección todavía en curso | `409 / admission.pending`, con `Retry-After` |
| Misma clave, otro contenido | `409 / idempotency.conflict` |
| Subida de archivo | Compara su SHA-256 por bloques, sin cargar todo el archivo en RAM |

## ¿Qué sucede cuando vence un resultado? {#expiration}

El estado de procesamiento `done` no implica que el archivo siga disponible. La retención temporal empieza al finalizar correctamente, no al solicitar ni al comenzar el trabajo.

@steps
| finish | Finalizar y fijar TTL | El resultado temporal recibe su fecha de caducidad; el persistente no caduca automáticamente. |
| expire | Rechazar nuevos accesos | Al comprobar el vencimiento, deja de emitir enlaces y rechaza nuevas transferencias. |
| protect | Proteger la lectura iniciada | Un lease mantiene la transferencia existente; hay límites de duración y concurrencia globales y por propietario. |
| delete | Limpiar cuando sea seguro | Espera a lectores y escritores; reintenta el borrado de carpetas gestionadas y marcadas. |

El lease se libera también ante `Range` inválido, desconexión, error de envío y timeout. El proxy debe limitar su buffering y sus tiempos. Terminar una respuesta HTTP no confirma que el usuario haya guardado el archivo.

La limpieza es eventual, no al milisegundo. No borra una copia ya descargada al navegador ni equivale a borrado forense del disco.

## ¿Cómo se detienen los escritores y se recupera un reinicio? {#recovery}

| Componente o límite | Comportamiento y alcance |
| --- | --- |
| FFmpeg | Se detiene y se espera su salida antes de borrar |
| Descargas | Observan cancelación en sus hooks; no se borra mientras siguen escribiendo |
| Inferencia nativa, yt-dlp y Demucs | Algunos pasos no se interrumpen de inmediato; se espera su salida aunque sobrepasen el plazo solicitado |
| Disco | Se comprueba en hooks y periódicamente; no es una cuota dura del kernel |
| Límites absolutos | Requieren aislamiento y límites adicionales del contenedor y del proxy |
| Parada forzada | Puede dejar trabajo interrumpido; el siguiente arranque reconcilia el estado |

## ¿Cómo se conservan el legado y las rutas anteriores? {#migration}

`STATE_DIR/history.db` conserva el JSON anterior con columnas indexadas de propietario, fecha y estado. La migración es versionada y crea una copia `history.db.v0-*.bak`; no sustituye la base anterior por una vacía.

Los trabajos sin propietario quedan como `admin:legacy`. No se mueven ni se borran sus medios. Sus copias de migración contienen historial persistente y las administra el operador.

Los trabajos nuevos conservan el modo y la raíz elegidos al crearse. Las rutas de resultados son relativas al espacio de trabajo. `media-roots.json` guarda solo raíces gestionadas, no nombres de medios ni URLs; permite recuperar raíces temporales anteriores que aún existan. El borrado no sigue enlaces simbólicos ni toca carpetas sin su marcador.

@steps
| fix-state | Antes de cambiar OUTPUT_DIR, fija STATE_DIR | En instalaciones locales antiguas, conserva primero la ubicación anterior: `OUTPUT_DIR.parent`. |
| stop-server | Si cambias el estado, detén el servidor | No migres mientras haya procesos escribiendo o transferencias activas. |
| copy-state | Respalda y copia el estado deliberadamente | Conserva configuración, historial, cuota y firma; ajusta permisos y comprueba la migración antes de reabrir. |
| preserve-keys | Conserva signing.key y admission.db | No los elimines para «reiniciar» el servicio. |

Los originales antiguos en `downloads/uploads` o `data/stems` son legado, no desechables asumidos. No se barren automáticamente. Revísalos manualmente con el servicio detenido si necesitas recuperar espacio.

## ¿Qué validan las pruebas y qué queda fuera? {#validation}

| Prueba | Evidencia que aporta | Lo que no sustituye |
| --- | --- | --- |
| `python -m pytest` | Pruebas con raíces desechables, aislamiento, retención y reservas concurrentes acotadas | Medición del throughput de producción |
| `node --test tests/test_ui.mjs` | Lógica del frontend | Interacción con un navegador o proxy real |
| Playwright | Descarga automática con actividad cerrada, no duplicación por polling, recarga o historial, botón manual y estados anónimos y caducados | Prueba del despliegue público |
| Integración de medios | FFmpeg y fuentes locales generadas, sin proveedores reales | Carga sobre sitios de medios externos |
| `tests/container_smoke.py` | Ambos modos con imagen, Python 3.11 y UID 10001; sin montar `data/` ni acceder a la red | Revisión del proxy, su buffering y sus tiempos |

El comando de la prueba de contenedor está en la cabecera de `tests/container_smoke.py`. Cambia `PERSIST_STORAGE=false` por `true` para comprobar el segundo modo. Las versiones mínimas de FastAPI, Starlette y multipart se ajustan a las verificadas para `Range` y cierre de subidas interrumpidas.

## ¿Qué hay que completar antes de publicar? {#publication}

@callout level=warning title="No exponer todavía a usuarios públicos no confiables"
| Las cuentas se posponen por decisión del operador. No se improvisan contraseñas ni un proveedor OIDC. La transcripción sigue reservada a administración. |
| No se han creado servicios cloud ni infraestructura distribuida. Un bloqueo de instancia impide varios procesos sobre el mismo estado. No añadas réplicas ni workers de Uvicorn a esta cola local. |

- [ ] Elige proveedor de cuentas y define asociación de trabajos, cuota de registrados, recuperación y revocación según ese mecanismo.
- [ ] Restringe la salida de red frente a SSRF, redirecciones y DNS rebinding. La validación inicial de URL no basta; añade CAPTCHA si el abuso lo exige.
- [ ] Configura HTTPS, cookies seguras, origen exacto y proxy confiable explícito. No registres tokens de enlaces; el contenedor no confía en cabeceras reenviadas por defecto.
- [ ] Mide trabajos por minuto, cola, p95, errores, CPU, RAM, disco y tráfico con fuentes controladas. Fija presupuestos, alertas y pruebas de saturación y recuperación.
- [ ] Comprueba `Range`, desconexiones, buffering y tiempos con el proxy real. Las pruebas ASGI no sustituyen esa verificación.
