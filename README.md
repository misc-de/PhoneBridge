# PhoneBridge

Your Linux phone (FuriOS, Phosh, Mobian …) in the panel of your desktop.

- **Panel icon** (StatusNotifierItem – XFCE "Status Tray", KDE, Cinnamon, MATE, waybar):
  battery level as a filling phone, a bolt while charging, a badge with unread SMS,
  details in the tooltip, a menu for the rest.
- **Window** (GTK 4 / libadwaita), opened from the icon:
  - *Overview*: battery, mobile network, Wi-Fi; mobile data, Wi-Fi, volume,
    ring profile, power profile, "ring the phone" to find it
  - *Phone*: dial pad, call history of GNOME Calls (read only), VoiceBox's messages
    when VoiceBox is installed, the call in progress
    with answer / hang up, and a notification with both for incoming calls; with the
    patched spa-droid plugin (droid-call-sink/-source), the call's sound on the PC:
    the caller on the PC's speakers, the PC's microphone on the line, the phone's
    microphone muted in the modem meanwhile
  - *Messages*: chatty's SMS conversations with profile pictures, reply, write new ones,
    call from the phone; a desktop notification for every new SMS
  - *Contacts*: all address books of the phone - look up, call, write to, add, change,
    delete, set a photo
  - *Appointments*: all calendars of the phone as month and agenda; add, change and
    delete appointments, with reminders; recurring ones as a series or a single day
  - *Phone settings*: common GNOME settings as switches, every other GSettings key
    through a search
- Several phones, one of them shown in the panel.

## How it works

Nothing is installed on the phone and no port is opened there. PhoneBridge starts
`python3` on the phone over SSH and feeds it `phonebridge/agent.py`; the agent answers
JSON lines on the same connection and reports changes by itself (UPower, ofono, chatty's
store). It needs Python 3 and PyGObject on the phone – both are there on FuriOS/Phosh.

| What | Source on the phone |
|---|---|
| Battery | UPower |
| Network, mobile data, incoming SMS | ofono |
| Sending SMS | ModemManager (like chatty) |
| SMS history | chatty's SQLite store, read only |
| Wi-Fi | nmcli |
| Volume | wpctl |
| Ring profile, ringing | feedbackd |
| Power profile | power-profiles (batman) |
| Settings | GSettings |
| Contacts, appointments | evolution-data-server over D-Bus (changes sync to the accounts) |
| Call history | GNOME Calls' records.db, read only |
| Calls in progress | ofono VoiceCallManager |

Messages sent from PhoneBridge are kept in `~/.local/share/phonebridge/sent.jsonl`
on the phone, because chatty does not list messages it did not send itself.

## Install

```sh
ssh-copy-id furios@<phone>      # once, if not done yet
./install.sh                    # into ~/.local, starts at login
phonebridge                     # or from the menu
phonebridge --calendar          # straight to a page: --phone, --messages, --contacts ...
```

Needs GTK 4, libadwaita, PyGObject and pycairo (Manjaro/Arch:
`pacman -S python-gobject libadwaita python-cairo`). Phones are added under
*Menu → Phones …*.

## Tests

```sh
tests/run-tests.sh
```

No phone, no network, no root: the agent runs locally against a made-up chatty
store, on a private session bus.
