# Credits

PhoneBridge is a thin layer over a lot of free software - on the PC and on the
phone. Thank you to everyone behind these projects.

## On the PC

| Project | Used for | License |
|---|---|---|
| [GTK 4](https://gtk.org) | the window and its widgets | LGPL-2.1+ |
| [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) | the GNOME look, dialogs, adaptive layout | LGPL-2.1+ |
| [PyGObject](https://pygobject.gnome.org) | GTK, GLib and D-Bus from Python | LGPL-2.1+ |
| [pycairo](https://github.com/pygobject/pycairo) | drawing the panel icon and the calendar | LGPL-2.1 / MPL-1.1 |
| [OpenSSH](https://www.openssh.com) | the only connection to the phone | BSD |
| [libsecret](https://gitlab.gnome.org/GNOME/libsecret) | passwords in the desktop's keyring | LGPL-2.1+ |
| [GStreamer](https://gstreamer.freedesktop.org) (good, bad, libav) | the phone's screen and the webcam | LGPL-2.1+ |
| [PipeWire](https://pipewire.org) / [WirePlumber](https://gitlab.freedesktop.org/pipewire/wireplumber) | sound of calls and music at the PC, the PipeWire camera | MIT |
| [FreeRDP](https://www.freerdp.com) | the client for the phone's desktop session (bundled in the Flatpak, see [NOTICE](NOTICE)) | Apache-2.0 |
| [v4l2loopback](https://github.com/umlaeute/v4l2loopback) | the virtual webcam | GPL-2.0 |
| [NetworkManager](https://networkmanager.dev) | joining the phone's hotspot | GPL-2.0+ / LGPL-2.1+ |

## On the phone

PhoneBridge installs nothing there; it reads and changes what these do:

| Project | Used for | License |
|---|---|---|
| [ofono](https://git.kernel.org/pub/scm/network/ofono/ofono.git) | network, mobile data, calls, incoming SMS | GPL-2.0 |
| [ModemManager](https://modemmanager.org) | sending SMS | GPL-2.0+ |
| [Chatty](https://gitlab.gnome.org/World/Chatty) | the SMS history | GPL-3.0+ |
| [GNOME Calls](https://gitlab.gnome.org/GNOME/calls) | call history and SIP accounts | GPL-3.0+ |
| [evolution-data-server](https://gitlab.gnome.org/GNOME/evolution-data-server) | contacts and appointments | LGPL-2.0 |
| [UPower](https://upower.freedesktop.org) | the battery | GPL-2.0+ |
| [feedbackd](https://source.puri.sm/Librem5/feedbackd) | ring profile, finding the phone | GPL-3.0+ |
| [power-profiles-daemon](https://gitlab.freedesktop.org/upower/power-profiles-daemon) | the power profile | GPL-3.0+ |
| [wf-recorder](https://github.com/ammen99/wf-recorder) | the phone's screen | MIT |
| [x264](https://www.videolan.org/developers/x264.html) | encoding screen and camera | GPL-2.0+ |
| [gst-droid](https://github.com/sailfishos/gst-droid) (`droidcamsrc`) | the phone's cameras | LGPL-2.1 |
| [python-evdev](https://github.com/gvalkov/python-evdev) | taps and keys from the PC | BSD-3-Clause |
| [grim](https://sr.ht/~emersion/grim/) | screenshots | MIT |
| [wl-clipboard](https://github.com/bugaevc/wl-clipboard) | the clipboard | GPL-3.0 |
| [GNOME Shell](https://gitlab.gnome.org/GNOME/gnome-shell) / [GNOME Remote Desktop](https://gitlab.gnome.org/GNOME/gnome-remote-desktop) | the phone's desktop session over RDP | GPL-2.0+ |
| [Phosh](https://phosh.mobi) and [FuriLabs](https://furilabs.com) / FuriOS | the phone it is made for | GPL-3.0+ / various |
