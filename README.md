# Tacx Smart Trainer + World of Warcraft Cycling App

Ride real (recorded) roads inside a private World of Warcraft server using a
Tacx smart trainer. Pedaling drives your in-game speed and steering; real
elevation drives trainer resistance. Everything is controlled from in-game
chat — no alt-tabbing needed once it's running.

---

## 1. How it all fits together

There are **three separate pieces**, running in three different places:

```
┌─────────────────────┐        /tmp bridge files        ┌──────────────────────┐
│   tacx_master.py     │ <---------------------------->  │   speedbridge.lua     │
│   (your Linux PC)    │        (position, speed,        │   (on the server)     │
│                       │         commands, responses)    │                       │
│  - Talks to the Tacx  │                                  │  - Reads your live    │
│    trainer over BLE   │                                  │    position/facing    │
│  - Runs the physics   │                                  │  - Applies speed via  │
│    model (power ->    │                                  │    .modify speed      │
│    speed, grade)      │                                  │  - Relays HUD data to │
│  - Steers your        │                                  │    the client addon   │
│    character via      │                                  │                       │
│    simulated key      │                                  │                       │
│    presses (xdotool)  │                                  │                       │
└──────────┬────────────┘                                  └───────────┬───────────┘
           │                                                            │
           │                                          addon message    │
           │                                       (SendAddonMessage)  │
           v                                                            v
   Your WoW game window                                    ┌───────────────────────┐
   (real client, being                                       │   TacxHUD addon        │
    steered/controlled)                                      │   (in the game client) │
                                                               │  - Shows speed, power, │
                                                               │    distance, gradient  │
                                                               └───────────────────────┘
```

**Why it's split up this way:** the game server (`speedbridge.lua`) can read
and change things about your character, but it can't talk to a Bluetooth
trainer or draw an on-screen panel. Your PC (`tacx_master.py`) can talk to
the trainer and simulate keyboard input, but has no way to read your
character's data directly except by asking the server. The addon
(`TacxHUD`) runs inside the actual game and can draw a panel, but only if
the server sends it data — client addons can't read files from your PC's
disk.

---

## 2. What you need installed

- **Python 3** with these packages (`pip install <name> --break-system-packages`):
  `bleak` (Bluetooth), `websockets` (unused now, safe to skip)
- **`xdotool`** (`sudo apt install xdotool`) — lets Python simulate key
  presses in the game window (steering, forward movement key)
- **`scrot`** (`sudo apt install scrot`) — takes automatic screenshots
- **An AzerothCore-based server** with the **ALE** module (AzerothCore Lua
  Engine) compiled in and enabled. This is what lets `speedbridge.lua` run
  on the server at all.

---

## 3. The three components, explained

### `tacx_master.py` — the brain, runs on your PC

Run this once per session and just leave it running in a terminal:

```bash
python3 tacx_master.py
```

It will:
1. Search for your WoW game window (must already be open)
2. Scan for and connect to your Tacx trainer over Bluetooth
3. Sit and wait for commands typed in-game

**What it does while a ride is active:**
- Reads your live power output from the trainer
- Reads your character's live position (from a file `speedbridge.lua` writes)
- Runs a physics model (power in, rolling resistance + air drag + gravity
  out) to compute a realistic speed
- Sends that speed to the server (via a file `speedbridge.lua` reads)
- Sends resistance commands to the trainer, based on the real recorded
  route's elevation at your current position
- Steers your character by calculating the correct heading and briefly
  simulating Left/Right arrow key presses — the same as a real player
  turning
- Writes live stats (speed, power, distance, gradient) for the in-game HUD
- Logs everything to `exports/` (for Strava) and `debug_logs/` (for
  diagnosing steering issues)

**You should never need to edit this file** except the tunable constants
near the top if something needs recalibrating (see Section 6).

### `speedbridge.lua` — the server-side bridge

Copy this into your server's `lua_scripts` folder (e.g.
`.../env/dist/bin/lua_scripts/speedbridge.lua`), then restart the
worldserver.

**Important:** near the top of this file is a line:
```lua
local MY_CHARACTER_NAME = "Shamishaman"
```
This **must exactly match your character's name** (case-sensitive). If you
create a new character or play on a server with Playerbots (fake AI
players), this filter is what stops the script from getting confused by
other characters — without it, bots would trigger the same logic as you
and scramble everything.

It runs a repeating check (once per second) that:
- Reports your exact live position to a file (for steering + progress)
- Applies whatever speed value Python has calculated (`.modify speed`)
- Relays HUD data to your client addon
- Handles one-time teleports (spawning you at a route's start)
- Listens for and responds to chat commands (see Section 4)

### `TacxHUD` — the in-game display addon

A normal WoW addon. Install by copying the whole `TacxHUD` folder into your
**client's** `Interface/AddOns/` folder (not the server). Enable it at the
character-select AddOns list if it isn't already.

Shows a small panel (drag it by the top to reposition) with:
- Speed (km/h)
- Power (W)
- Distance ridden (m)
- Distance to go (m)
- Gradient (%, colored: gray = flat/downhill, green → red = increasingly
  steep climb)

---

## 4. Daily usage — everything from in-game chat

Type these directly in normal chat (not console/GM commands, just plain
chat — the server intercepts specific phrases):

| Command | What it does |
|---|---|
| `help` | Lists all commands |
| `routes` | Lists every route file in your `routes/` folder |
| `startride <name>` | Teleports you to that route's start and begins the ride |
| `stopride` | Stops the current ride, resets speed/resistance, saves your ride export |
| `startroute` | Begins recording a new route — walk/ride normally after this |
| `stoproute` | Stops recording |
| `saveroute <name>` | Converts your last recording into a real, usable route file |
| `snap` | Takes a screenshot right now (also happens automatically at ride start/finish) |

**Typical session:**
```
python3 tacx_master.py        # start this once, leave it running
```
In-game:
```
startride my_favorite_road
```
Pedal. When done:
```
stopride
```

**Recording a brand new route:**
```
startroute
```
(walk or ride the road you want to record, at any speed — even sped up
with `.modify speed X` if you want to record faster than real-time)
```
stoproute
saveroute my_new_road
routes                         (confirm it shows up)
startride my_new_road          (try it out)
```

---

## 5. Where everything lives

| What | Where |
|---|---|
| Route files (`.json`) | `routes/` folder, next to `tacx_master.py` |
| Finished ride exports (`.tcx` for Strava, screenshots) | `exports/` folder |
| Per-ride steering diagnostics (`.csv`) | `debug_logs/` folder |
| Live communication between Python and Lua | `/tmp/tacx_*` files (temporary, safe to ignore/delete when nothing is running) |

**Uploading a ride to Strava:** go to strava.com/upload and drag in the
`.tcx` file from `exports/`. Optionally drag the matching screenshot(s)
onto the activity afterward.

---

## 6. Tunable constants (all near the top of `tacx_master.py`)

You generally shouldn't need to touch these, but if something feels off:

| Constant | What it controls |
|---|---|
| `MY_CHARACTER_NAME` (in `speedbridge.lua`) | Your exact character name — **must be correct** |
| `WOW_BASE_RUN_SPEED_KMH` | Calibration: what "normal" in-game speed equals in km/h |
| `TOTAL_MASS_KG`, `CRR`, `CDA` | Physics model inputs (rider+bike weight, tire rolling resistance, aerodynamic drag) — control how power translates to speed |
| `DOWNHILL_SPEED_DAMPING` | How much descents are allowed to build virtual speed (trainer resistance is unaffected either way — this only caps how fast you go, not how hard the hill feels) |
| `TURN_RATE_RAD_PER_SEC` | How fast your character physically turns — calibrated once, shouldn't need changing |
| `DAMPING_FACTOR` | How gently steering corrections are applied |
| `LOOKAHEAD_DISTANCE_M` | Baseline steering lookahead distance |

---

## 7. Troubleshooting

**"routes" or "startride" don't respond, or take a long time:**
Check your server's console for how many bots/players are online — a
heavily loaded server (e.g. hundreds of Playerbots) can make everything
sluggish. Try lowering `AiPlayerbot.MaxRandomBots` in `playerbots.conf` if
you have that module installed.

**Speed/HUD/steering behaves erratically, seemingly at random:**
Almost always means `MY_CHARACTER_NAME` in `speedbridge.lua` doesn't
exactly match your character, and something else (often a bot) is
triggering the logic instead of you.

**`xdotool` can't find the WoW window:**
Run `xdotool search --name "^World of Warcraft$"` yourself in a terminal
to check the exact window title. Terminal windows with WoW-related folder
names in their title can sometimes get matched by mistake — this is why
the script uses an anchored pattern (`^...$`) rather than a loose search.

**Character veers off the recorded path / corners feel wrong:**
This has been an ongoing tuning process. If it happens again, the
`debug_logs/` CSV from that exact ride is the most useful thing you can
share — it records the exact target point, heading, and correction the
algorithm computed every single tick, which is far more diagnostic than
just describing what you saw.

**HUD panel shows nothing:**
Confirm `speedbridge.lua` is actually running server-side (restart the
worldserver after copying in a new version) and that `tacx_master.py` is
connected and a ride is active — the HUD only updates during an active
ride.

---

## 8. Not built yet (possible future additions)

- Automatic Strava upload via their API (currently: manual drag-and-drop of
  the exported `.tcx` file)
- A custom bicycle mount/model (currently riding as your normal character)
- A more polished, WoW-styled route-selection UI (currently: chat-based)
