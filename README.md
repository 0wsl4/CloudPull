# CloudPull

Descarga **todos los álbumes** de iCloud Fotos a una carpeta local, uno a uno, automatizando
la web de iCloud con Playwright. Si un álbum falla, anota el error en un log y sigue con el
siguiente. Opcionalmente verifica que MEGAsync ha subido cada álbum.

> Apple no ofrece una API oficial para esto y `icloudpd` puede provocar bloqueos de cuenta
> (`-20209`). Este proyecto no usa tu contraseña: inicias sesión tú mismo en una ventana de
> Chrome y la sesión se guarda en un perfil local en tu equipo.

## Requisitos

- Windows (probado), Python 3.10+ y Google Chrome instalado.
- `pip install -r requirements.txt` y `playwright install chromium`.
- La interfaz de iCloud debe estar en **español** (los selectores usan textos como «elementos» o «Descargar»).

## Uso

1. `python migrate_icloud.py --setup` — te pregunta la cuenta de iCloud y la carpeta de destino
   y las guarda en `config.json` (no se sube a Git).
2. `python migrate_icloud.py --login` — abre una ventana para iniciar sesión en iCloud.
3. `python migrate_icloud.py --inventory` — lista tus álbumes.
4. `python migrate_icloud.py --all` — descarga todos los pendientes.

En Windows puedes usar `migrar_icloud.bat`, que ofrece todo esto en un menú.

Otros comandos: `--pending`, `--album "Nombre"`, `--choose-album`.

## Qué hace con cada álbum

- Selecciona los elementos uno a uno (incluidos álbumes de más de ~90 elementos, donde iCloud virtualiza la rejilla) y comprueba que el total coincide con el inventario antes de descargar.
- Valida el ZIP (integridad y rutas duplicadas), lo extrae en `<destino>/<álbum>` y lo borra al terminar.
- Los álbumes cuya carpeta ya existe en el destino se saltan; `skip_folders` en `config.json` permite indicar otras carpetas con álbumes ya descargados.
- Con `verify_megasync: true` compara los archivos locales con la caché de MEGAsync y reintenta hasta 30 min.

## Registros y privacidad

- `logs/` guarda el log de cada sesión y `logs/fallos_albumes.log` los álbumes que han fallado.
- `config.json`, `inventory.json`, `pending.json` y `logs/` están en `.gitignore`: contienen tu cuenta y los nombres de tus álbumes.

## Limitaciones

- Depende de la interfaz web de iCloud: un cambio de Apple puede romper los selectores.
- Los álbumes con nombre repetido en iCloud no se distinguen.
- Uso bajo tu responsabilidad y solo con tu propia cuenta.

## Licencia

[MIT](LICENSE).
