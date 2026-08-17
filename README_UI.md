# Oberfläche

Die Oberfläche ist deutsch und hat zwei Ebenen:

## Übersicht

Die Übersicht ist für den täglichen Betrieb gedacht. Sie zeigt 24 Streams in einem gut lesbaren Raster mit:

- Name
- kurzer Status
- Stream-Link
- Bearbeiten
- Neu starten
- Herunterfahren

Technische Details und Update-/Recovery-Meldungen bleiben aus dieser Ansicht heraus.

**Bearbeiten** öffnet einen Dialog. `Abbrechen`, `Schließen` und `Esc` schließen ihn immer – auch wenn noch keine Stream-URL eingetragen ist. Eine leere URL ist erlaubt und bedeutet „kein Stream konfiguriert“.

## Adminbereich

Der Adminbereich wird erst nach einer Warnung geöffnet. Er hat bewusst mehr Abstand und stärkeren Kontrast zwischen Seitenhintergrund, Stream-Karten, Technikbereich und globalen Aktionen.

Zusätzlich verfügbar sind u. a.:

- Start / Stop / Logs / Statusprüfung
- vollständige SSH-/Qualitätskonfiguration
- VLC-/Streamlink-Updates
- OverlayFS-Recovery
- globale Wartungsaktionen

## Browser-Titel und Favicon

Titel und Favicon zeigen das Verhältnis der erreichbaren Pis mit laufendem Stream-Wächter zur Gesamtzahl, z. B.:

```text
00x/24x Streaming Setup
17x/24x Streaming Setup
24x/24x Streaming Setup
```

Der Projektname selbst bleibt **Streaming Setup**.

## Master node behavior

In Admin, Stream 01 / `.101` is marked **MASTER**. Its filesystem status is considered healthy when it is writable. Its per-node **VLC + Streamlink lokal aktualisieren** button performs only the writable master's local package update: Stream 01 is stopped/restored as needed, with no OverlayFS toggle and no reboot. `Alle aktualisieren` updates the 23 workers with their normal OverlayFS workflow and then performs the same local VLC + Streamlink update on the master.

Fleet reboot/shutdown always performs the master last so the controller remains available while worker operations are being completed.

Per-node and fleet software updates preserve the desired running/stopped state. A stream that was intentionally stopped stays stopped after its package update.

## URL changes while a stream is retrying

Saving a changed stream URL always reapplies the runtime configuration. A short
in-flight status/start SSH command is allowed to finish first; then the old
watcher/process group is replaced by a fresh `start.sh` generation using the new
URL. Reboot/shutdown/update operations still block this action intentionally.

The simple overview also exposes retry/wait/cooldown details so unattended
recovery is visible without opening the Admin view.
