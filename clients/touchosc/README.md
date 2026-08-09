# TouchOSC surface

`softcut.tosc` is a TouchOSC layout that drives softcut-py over the OSC server
in [`softcut.osc`](../../docs/guide/osc.md). Every control on it sends a real
address from the softcut namespace, so the same layout also drives the
standalone [`softcut-osc`](../softcut-osc/) binary -- the two share their
defaults and their conventions, and nothing about the layout is specific to the
Python host.

softcut-lib's own `softcut_jack_osc` demo client answers the same addresses but
not the same conventions: its pan is `0..1` rather than `-1..1`, and it resets
output level to `0` and `phase_quant` to `1` where these default to `1` and `0`.
The controls would reach it; the pan fader and the resting positions would lie
about it.

It is generated rather than drawn: `build_layout.py` builds the whole surface
with [py2tosc](https://pypi.org/project/py2tosc/), which means the layout can be
reshaped for a different canvas, voice count or parameter range by rerunning it
rather than by clicking through the editor. Rebuilding also writes `softcut.xml`
beside it -- the same layout in TouchOSC's readable export, worth opening when
you want to see what a control actually carries. It is git-ignored, at ~2 MB of
generated markup for 817 controls; pass `--no-xml` to skip writing it.

## Using it

Start a server on the machine with the audio interface:

```bash
python -m softcut.osc                     # listens on UDP 9999
```

Then, in TouchOSC (Hexler's MK2 app -- this is the `.tosc` format, not the older
TouchOSC Mk1), open `softcut.tosc` and set **connection 1** to the server's
address with send port `9999`. Every binding is on that slot alone, out of the
ten TouchOSC offers, so a second destination configured later gets nothing from
this surface unless you widen `CONNECTION` in the generator and rebuild. The
surface is send-only apart from the phase readout, so connection 1 is all that
is needed to play it.

To light up the phase readout on the `sync` page, tell the server where to
reply -- the tablet's address and whatever receive port the TouchOSC connection
is set to -- then press `poll on`:

```bash
python -m softcut.osc --reply-host 192.168.1.42 --reply-port 9000
```

## Pages

| page | what is on it |
|---|---|
| `mix` | a strip per voice: level, pan, rate, and the play / rec / loop flags |
| `loop` | loop flag, loop start and end, position, rate, rate slew, fade time |
| `rec` | rec / play / rec-once flags, rec and pre levels, rec offset, recpre slew |
| `pre` | the pre-filter: cutoff, cutoff mod, rq, and the LP/HP/BP/BR/dry mix |
| `post` | the post-filter: cutoff, rq, and the LP/HP/BP/BR/dry mix |
| `routing` | level, pan, enable, input level, buffer assignment, feedback matrix |
| `sync` | phase quantisation and offset, voice-sync matrix, phase readout, poll |
| `system` | reset, buffer clears, disk read and write, hello, quit |

The two matrices read row-to-column: a cell in the feedback matrix routes the
row's voice into the column's voice, and a cell in the voice-sync matrix cuts
the row's voice to the column's position. Syncing a voice to itself does
nothing, so the sync diagonal is a blank rather than a button.

Controls open where a freshly reset voice actually sits -- level up, pan
centred, rate at 1, the post-filter dry and the pre-filter low-passed -- so the
surface agrees with the engine before anything is touched. TouchOSC transmits
nothing on load, so those positions are a display and not a preset: the first
control you move is the first thing the engine hears.

Voices are numbered 1-6 on the surface, the way norns numbers them, and 0-5 on
the wire, the way the protocol requires. The translation is baked into each
control.

## How a control becomes a message

A softcut address takes the voice as its first argument, so each control sends
two: a constant integer naming its voice, and its own position scaled into the
parameter's range. The `rate` fader for voice 3, at the top of its travel:

```
/set/param/cut/rate  2  2.0
```

Buttons work the same way. A latching flag sends `1.0` when it goes on and `0.0`
when it goes off; a momentary button (buffer assignment, voice sync, and
everything on `system`) fires once on the press and carries only constants.

The phase readout is the one thing that listens rather than talks: its faders
receive `/poll/softcut/phase`, and the constant voice argument on each is what
picks that voice's reply out of the shared address.

## Regenerating

```bash
pip install py2tosc
python clients/touchosc/build_layout.py            # or: make touchosc
```

| option | |
|---|---|
| `-o, --output` | where to write; the extension picks `.tosc` or `.xml` |
| `--no-xml` | skip the `.xml` export written alongside a `.tosc` |
| `--voices` | how many voices, 1 to 6 |
| `--size` | canvas size as `WIDTHxHEIGHT`, default `1024x768` |
| `--max-time` | seconds spanned by the loop point, position and phase controls |
| `--rate` | rate faders span `-RATE..RATE`, default 2 |
| `--read-file`, `--write-file` | the paths the disk buttons carry |

The generator refuses to write a layout with validation errors, and
`tests/test_touchosc.py` pushes every binding in it through the server's own
dispatch table, so an address or argument that stops matching the server is a
test failure rather than a control that quietly does nothing. The same test
checks the reverse -- every address the server handles has a control on the
surface, except `/goodbye` (a synonym for `/quit`), the two VU polls and the two
slew-time parameters, which the server accepts and ignores.

## Limits worth knowing

- **Ranges are linear.** A TouchOSC argument scales linearly between two
  bounds, so filter cutoff sweeps linearly in Hz rather than by ear. Narrow
  `--max-time` to the material you are looping and the loop-point faders get
  proportionally finer. Each filter's cutoff fader tops out at that filter's own
  default -- 16 kHz on the pre, 12 kHz on the post -- so neither opens below
  where the engine already is.

- **Disk paths are constants.** A message can only read values from the control
  it belongs to, so the read and write buttons carry the paths given at build
  time. Pass `--read-file` and `--write-file`, or edit the two constants in the
  TouchOSC editor.

- **Two addresses are partial**, in the server rather than here: `/set/enabled/cut`
  is approximated with the play flag, and `/set/level/in_cut` ignores the input
  channel because the binding has one scalar input gain per voice. The `enabled`
  row and the `input -> cut` row inherit both.

- **`quit` stops the server.** It is on the far right of `system`, and there is
  no confirmation step available on a TouchOSC button.
