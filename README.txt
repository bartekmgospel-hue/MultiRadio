RadioSpotify Multi v1.0
=======================

Obsługiwane stacje i identyfikatory odsluchane.eu:

nowyswiat  -> Radio Nowy Świat -> r=105
chillizet  -> Chillizet        -> r=40
radiozet   -> Radio ZET        -> r=1
trojka     -> Trójka           -> r=48
czworka    -> Czwórka          -> r=49

Tworzone playlisty Spotify:
- Radio Nowy Świat
- Chillizet
- Radio ZET
- Trójka
- Czwórka

Każda playlista:
- max 5000 unikalnych utworów,
- LIVE dodaje najnowsze na początek,
- BACKFILL uzupełnia archiwum,
- osobny state:
  .radio_state_nowyswiat.json
  .radio_state_chillizet.json
  .radio_state_radiozet.json
  .radio_state_trojka.json
  .radio_state_czworka.json

INSTALACJA
----------
Do głównego katalogu repozytorium:
  sync_odsluchane_station.py
  requirements.txt

Do:
  .github/workflows/

wgraj:
  radio-multi-live.yml
  radio-multi-backfill.yml

Sekrety Spotify są te same co przy 753Radio:
  SPOTIFY_CLIENT_ID
  SPOTIFY_CLIENT_SECRET
  SPOTIFY_REFRESH_TOKEN

TEST
----
GitHub -> Actions -> Radio Multi LIVE -> Run workflow
i wybierz jedną stację, np. radiozet.

CRON-JOB.ORG — LIVE
-------------------
Endpoint:
https://api.github.com/repos/TWOJ_LOGIN/753Radio/actions/workflows/radio-multi-live.yml/dispatches

POST body dla poszczególnych stacji:

Radio Nowy Świat:
{"ref":"main","inputs":{"station":"nowyswiat"}}

Chillizet:
{"ref":"main","inputs":{"station":"chillizet"}}

Radio ZET:
{"ref":"main","inputs":{"station":"radiozet"}}

Trójka:
{"ref":"main","inputs":{"station":"trojka"}}

Czwórka:
{"ref":"main","inputs":{"station":"czworka"}}

Nagłówki takie same jak przy 753Radio.

ZALECANY START — LIVE
---------------------
Ze względu na wspólną quota Spotify zacznij od 1 uruchomienia na godzinę,
z rozłożeniem stacji w czasie:

Nowy Świat: minuta 05
Chillizet:   minuta 17
Radio ZET:   minuta 29
Trójka:      minuta 41
Czwórka:     minuta 53

Po kilku dniach, jeśli nie pojawia się QUOTA_EXCEEDED, można zejść do 30 min.

BACKFILL
--------
Endpoint:
https://api.github.com/repos/TWOJ_LOGIN/753Radio/actions/workflows/radio-multi-backfill.yml/dispatches

Body są identyczne jak wyżej, zmienia się tylko workflow URL.

Zalecenie początkowe: każda stacja 1x dziennie, najlepiej w innych godzinach.
BACKFILL ma limit 10 wyszukiwań Spotify na run, aby nie zjadać quota LIVE.

UWAGA
-----
Nie usuwaj .753radio_state.json — obecna playlista 753Radio pozostaje
niezależna od nowych stacji.
