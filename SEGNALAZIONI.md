# Segnalazioni

Cose che non funzionano, scomode, che danno risultati sbagliati o che mancano,
annotate da chi monta (strumento MCP `report_issue`). Una voce risolta si cancella.

Tipi: `bug` errore o crash, `scomodo` funziona ma costa troppi passi,
`sbagliato` risultato diverso da quello promesso, `manca` serve e non c'e',
`lento` troppo tempo per quello che fa.

## [bug] render_video / verify_edit (graph.py video_clip: input -ss + tpad) — 2026-11-18 20:22

Rendered files contain long black stretches while preview_frame shows the frames correctly. Reproducible with verify_edit: a video media clip that starts after the start of the rendered range (so it gets tpad=start_duration>0) and has in_>0 (input opened with -ss) comes out black for all or most of its window, often from mid-clip to the end of the range. The same clip renders fine when the range starts exactly at the clip start (tpad 0). It is independent of the source (VFR WhatsApp mp4 and a CFR vedit export both fail: test clip new2 project.mp4 in=2 start=18.3 in range 15.1-21.2 -> 100% black, with or without a color effect), and independent of zoompan, shake, motionblur or audio effects (all removed, same result). Image, text and html clips with tpad render fine. Environment: macOS arm64, ffmpeg 7.1.1 (Homebrew), render in pieces (24.3 s > SOGLIA_PEZZI_S). Results also look identical across edits for the same range, so a stale piece cache may be involved too (clear_cache did not change them).

- **compito:** Export a 24 s 9:16 edit (12 media clips on V1 + text/image/html overlays)
- **proposta:** Position media clips without tpad (e.g. setpts=PTS+start/TB with overlay eof_action=pass, or -itsoffset), or test with ffmpeg 8; have verify_edit/render report black pieces and re-render them with a single graph.
