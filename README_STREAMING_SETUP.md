# Streaming Setup – aktuelle Besonderheiten

## Standardkonfiguration

`nodes.default.json` enthält 24 Standardeinträge; beim ersten Start wird daraus lokal `nodes.json`:

- `Stream 01` → `pi@192.168.0.101`
- …
- `Stream 24` → `pi@192.168.0.124`

Die Stream-URLs sind absichtlich leer. Sie werden in der Übersicht über **Bearbeiten** eingetragen.

## Übersicht

Die normale Ansicht zeigt weiterhin für jeden Pi den aktuellen Streamstatus. Name und Stream-Link können über **Bearbeiten** geändert werden. Neustart und Herunterfahren bleiben sichtbar.

## Adminbereich

Beim Wechsel in den Adminbereich muss zuerst eine deutsche Warnung bestätigt werden. Die Bestätigung ist nur eine Bedienbarriere und keine Benutzer-Authentifizierung. Nach einem Neuladen startet die Seite wieder in der normalen Übersicht.

Globale Update-, Recovery- und Wartungsfehler werden nur im Adminbereich angezeigt.

## Dynamisches Favicon

Browser-Titel und Favicon zeigen die Zahl der aktuell per SSH erreichbaren Pis mit laufendem Stream-Supervisor als Verhältnis, z. B. `00x/24x`, `17x/24x` oder `24x/24x`. Sie werden nach den regelmäßigen Statusprüfungen automatisch aktualisiert.

## SSH

Der Controller verwendet nur Passwort-Authentifizierung und ignoriert SSH-Host-Keys. Damit gibt es nach Klonen/Reimaging keine Host-Key-Konflikte innerhalb des Controllers.
