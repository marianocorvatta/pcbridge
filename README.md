# PCBridge

Transferí archivos y texto entre computadoras de tu red local, desde el navegador.

Un único script de Python, sin dependencias. Funciona en Linux y macOS.

## Características

- **Descubrimiento automático** de los otros equipos de la red.
- **Archivos** de cualquier tamaño, con arrastrar y soltar.
- **Texto** con un botón para copiarlo en el otro equipo.
- **Clave compartida opcional**: si se usa, solo se reciben envíos de quien tenga la misma clave.

## Uso

Requiere Python 3. En cada computadora:

```bash
python3 pcbridge.py
```

Abrí **http://localhost:8765**, elegí el destino y mandá texto o archivos.
Lo recibido se guarda en `~/PCBridge`.

### Opciones

```bash
python3 pcbridge.py --name "MacBook"     # nombre visible para los demás
python3 pcbridge.py --token miclave      # exige la misma clave en ambos equipos
```

### Desde la terminal

```bash
python3 pcbridge.py send foto.jpg informe.pdf
python3 pcbridge.py text "hola"
echo "algo" | python3 pcbridge.py text
```

Si no se encuentra el destino automáticamente: `--to 192.168.1.50`.

## Red y firewall

| Puerto | Protocolo | Uso |
|---|---|---|
| 8765 | TCP | Transferencias e interfaz web |
| 8766 | UDP | Descubrimiento de equipos |

Si usás `ufw`, habilitá solo tu red local:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 8766 proto udp
```

En macOS, aceptá las conexiones entrantes para Python la primera vez.

## Seguridad

- La interfaz y los archivos recibidos solo se pueden abrir desde el propio equipo (`localhost`).
- Los demás equipos solo pueden *enviar*; no pueden leer nada.
- El tráfico no está cifrado: pensado para redes domésticas de confianza.
