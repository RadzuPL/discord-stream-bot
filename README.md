# discord-stream-bot

**[English](#english) · [Polski](#polski)**

---

## English

[Przejdź do wersji polskiej ↓](#polski)

A tiny Discord bot that plays an audio stream in one voice channel (for example an Icecast station from DeadAir) — but only while somebody is actually listening. By default it also leaves the channel when nobody is there, so the server icon does not show a permanent voice call. No commands, no privileged intents. All configuration is done through environment variables.

The image is built automatically (GitHub Actions) and published to GHCR: `ghcr.io/radzupl/discord-stream-bot:latest`. The repository is public, so the image can be pulled without logging in.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token from the Discord Developer Portal (Bot page) |
| `VOICE_CHANNEL_ID` | yes | ID of the voice channel (Developer Mode → right-click the channel → Copy Channel ID) |
| `STREAM_URL` | yes | Stream address, e.g. `http://deadair:80/live.mp3` or `http://192.168.10.15:8084/live.mp3` |
| `IDLE_TIMEOUT` | no | Seconds the channel may stay without listeners before the stream stops (and, with `LEAVE_WHEN_EMPTY=true`, the bot leaves), default `60`. `0` = always play and never leave |
| `LEAVE_WHEN_EMPTY` | no | `true` (default) = the bot leaves the voice channel once `IDLE_TIMEOUT` runs out and comes back when somebody joins. `false` = the bot stays in the channel all the time, silent while nobody listens |
| `STREAM_IPV4` | no | `true` (default) = for a plain `http://` stream the bot looks up the host's IPv4 address itself and hands ffmpeg the address (with the original `Host` header). This avoids multi-second IPv6 DNS timeouts on Docker container names. `false` = ffmpeg resolves the name itself. HTTPS streams are never rewritten |
| `VOLUME` | no | Volume multiplier, default `1.0` |
| `LOG_LEVEL` | no | `DEBUG` / `INFO` / `WARNING` / `ERROR`, default `INFO` |
| `FFMPEG_BEFORE_OPTIONS` | no | Overrides the ffmpeg input options (default: automatic stream reconnect) |
| `HEALTH_FILE` | no | Marker file for the healthcheck, default `/tmp/streambot.healthy` |

`LEAVE_WHEN_EMPTY` and `STREAM_IPV4` also accept `1`/`0`, `yes`/`no`, `on`/`off`.

The container exposes no ports. It only needs to be able to reach `STREAM_URL` (same Docker network, or a host address with a port).

### Bot permissions in Discord

OAuth2 scopes: `bot` (+ `applications.commands` if the same token is also used by DeadAir's text plugin).

Permissions for streaming alone: **View Channels**, **Connect**, **Speak** — permissions integer `3146752`. If the same token also serves DeadAir's text plugin, add **Send Messages** and **Read Message History** — integer `3214336`.

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot%20applications.commands&permissions=3214336
```

The voice bot needs no Privileged Gateway Intents. Message Content Intent is only needed by the text plugin, if you use it.

### Running: Docker Compose

```yaml
services:
  discord-stream-bot:
    image: ghcr.io/radzupl/discord-stream-bot:latest
    container_name: discord-stream-bot
    restart: unless-stopped
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN:?}
      VOICE_CHANNEL_ID: "123456789012345678"
      STREAM_URL: http://deadair/live.mp3
      LEAVE_WHEN_EMPTY: "true"
```

### Running: Unraid

`unraid/discord-stream-bot.xml` is a container template. It gives you a form with descriptions for every variable, a masked token field and an icon. By default it uses the `docker_network` network and a radio icon — adjust both in the form or in the file.

Unraid stores user templates as `my-<name>.xml` files in `/boot/config/plugins/dockerMan/templates-user/`, so installing the template is just downloading a file there.

#### Fresh install

1. On Unraid (Terminal or SSH), download the template:

   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml \
     https://raw.githubusercontent.com/RadzuPL/discord-stream-bot/main/unraid/discord-stream-bot.xml
   ```

2. **Docker** tab → **Add Container** → pick `my-discord-stream-bot` from the **Template** list (*User templates* section).
3. Fill in the three fields: token, voice channel ID, stream URL. Change the network (and optionally set a fixed IP) if needed.
4. **Apply**. After a few seconds the container log shows the idle-mode line. With `LEAVE_WHEN_EMPTY=true` the bot only joins (`Joining #<channel>`) once somebody is in the channel.

#### Swapping the template of an already running container

If the container already exists (e.g. created by hand) you do not have to delete it. Unraid matches a container to its template by name, and a hand-made container called `discord-stream-bot` has its own `my-discord-stream-bot.xml`:

1. Back up the old template:

   ```bash
   cp /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml /boot/config/my-discord-stream-bot.xml.bak
   ```

2. Overwrite it with the version from this repo (the same `wget -O ...` command as above).
3. **Docker** tab → click the container → **Edit**. Check the token, channel ID, stream URL, network and IP. If the form loaded empty or default values, fill them in from the `.bak` file or from your previous `docker run`.
4. **Apply** recreates the container with the new values.

If the form misbehaves: remove just the container (the image stays), restore the `.bak` file or add the container from scratch as described under *Fresh install*.

#### Updating the template

When the template changes in the repo, run the `wget -O ...` command again. Updates of the image itself show up in the Docker tab as usual (*update ready*). New optional variables (like `IDLE_TIMEOUT` or `LEAVE_WHEN_EMPTY`) work with their defaults even if your container does not define them — so after an image update the bot leaves an empty channel unless you set `LEAVE_WHEN_EMPTY=false`.

### How it works

- **Idle mode (default).** The stream plays only while at least one person who can actually hear it is in the channel (not a bot, not deafened). When the channel empties, the stream keeps playing for `IDLE_TIMEOUT` seconds (default 60) in case somebody comes back, then it stops. An empty channel means no stream traffic, and the station sees no listener either, so a station set to broadcast only while somebody is listening (e.g. DeadAir's playout setting) can go to sleep too. If the station does sleep, the first seconds after someone joins may be silent while it wakes up. Set `IDLE_TIMEOUT=0` to play continuously.
- **Leaving the channel (`LEAVE_WHEN_EMPTY=true`, default).** When the stream stops, the bot also disconnects from voice, so Discord no longer shows the server as having an active voice call. It keeps watching the channel (voice events reach it without being connected) and joins again as soon as somebody enters — usually within a few seconds; right after leaving it waits up to 5 s before rejoining, which avoids a discord.py reconnect loop. With `LEAVE_WHEN_EMPTY=false` the bot sits in the channel all the time and is just silent while nobody listens.
- **Fast start.** When the bot has to join, it opens the stream first and joins the voice channel while the stream is already connecting and buffering (up to 5 s ahead), so the voice handshake (a few seconds with DAVE) and the stream start overlap instead of adding up. The log shows the real timings: `Opening stream from …`, then `Stream is delivering audio X s after opening it` (connection and ffmpeg) and `Stream became audible X s after opening it` (first non-silent audio — an audience-gated station sends silence until it is back on air). If the second number is much bigger than the first, the delay is the station waking up, not the bot.
- A supervisor loop re-checks every 5 s that the bot is where it should be (in the channel while somebody listens; with `LEAVE_WHEN_EMPTY=false` always) and that playback matches the situation. If not, it rejoins / reconnects / restarts the stream.
- A dead stream is retried with growing delays (2, 4, 8 … up to 60 s), so it neither floods the logs nor hammers the server.
- If someone disconnects the bot or moves it to another channel, it returns to the target channel — immediately with `LEAVE_WHEN_EMPTY=false`, or while somebody is listening there with `LEAVE_WHEN_EMPTY=true`.
- The healthcheck is green while the bot is either playing audio for listeners or idle on purpose (nobody to play for — silent in the channel, or out of it with `LEAVE_WHEN_EMPTY=true`). An unreachable stream while somebody is listening = `unhealthy`.
- Since 1 March 2026 Discord requires end-to-end encryption (DAVE) in voice channels. It is supported by discord.py ≥ 2.7 together with the `davey` package; both are in `requirements.txt`. If the startup log says `davey MISSING`, voice will not work.
- Stage channels are not supported, only regular voice channels.

### Troubleshooting

| Symptom | What to check |
|---|---|
| `Missing required environment variable` | `DISCORD_TOKEN`, `VOICE_CHANNEL_ID` or `STREAM_URL` is missing |
| `LEAVE_WHEN_EMPTY must be true or false` | Typo in the value; use `true` or `false` |
| `Forbidden` / `Missing Access` on startup | The bot lacks View Channels / Connect on that channel |
| Bot is not in the channel | Normal with `LEAVE_WHEN_EMPTY=true` when nobody else is there (a deafened person does not count). It joins when somebody enters. Set `LEAVE_WHEN_EMPTY=false` to keep it there |
| Bot sits in the channel and is silent | Normal with `LEAVE_WHEN_EMPTY=false` when nobody else is there (a deafened person does not count). Otherwise: the Speak permission, the `davey` line in the log, whether `STREAM_URL` is reachable from the container |
| `Stream ended after 0s` in a loop | Wrong address or stream unavailable; set `LOG_LEVEL=DEBUG` to see ffmpeg output |
| Music starts many seconds after the bot joins | Compare the two timing lines in the log. `delivering audio` late = connection/DNS (also look for `IPv4 lookup for the stream host took …`); `became audible` much later than `delivering` = the station was off air and is warming up |

### Development

A push to `main` builds and publishes `latest` and a `sha-<short hash>` tag; a `v1.2.3` tag also publishes `1.2.3`. Once a week (Monday) the image rebuilds itself to pick up fresh dependencies.

### License

MIT — see [LICENSE](LICENSE). The container image also bundles third-party software (Python, ffmpeg, discord.py, davey and others), each under its own license.

---

## Polski

[Switch to the English version ↑](#english)

Mały bot, który gra strumień audio na jednym kanale głosowym Discorda (np. stację Icecast z DeadAir) — ale tylko wtedy, gdy ktoś faktycznie słucha. Domyślnie wychodzi też z kanału, gdy nikogo na nim nie ma, więc ikonka serwera nie pokazuje bez przerwy trwającej rozmowy. Bez komend i bez uprawnień uprzywilejowanych. Cała konfiguracja to zmienne środowiskowe.

Obraz buduje się automatycznie (GitHub Actions) i ląduje w GHCR: `ghcr.io/radzupl/discord-stream-bot:latest`. Repo jest publiczne, więc obraz ciąga się bez logowania.

### Zmienne środowiskowe

| Zmienna | Wymagana | Opis |
|---|---|---|
| `DISCORD_TOKEN` | tak | Token bota z Discord Developer Portal (strona Bot) |
| `VOICE_CHANNEL_ID` | tak | ID kanału głosowego (tryb dewelopera → prawy klik na kanale → Copy Channel ID) |
| `STREAM_URL` | tak | Adres strumienia, np. `http://deadair:80/live.mp3` albo `http://192.168.10.15:8084/live.mp3` |
| `IDLE_TIMEOUT` | nie | Ile sekund kanał może być bez słuchaczy, zanim strumień się zatrzyma (a przy `LEAVE_WHEN_EMPTY=true` bot wyjdzie); domyślnie `60`. `0` = gra bez przerwy i nigdy nie wychodzi |
| `LEAVE_WHEN_EMPTY` | nie | `true` (domyślnie) = po upływie `IDLE_TIMEOUT` bot wychodzi z kanału głosowego i wraca, gdy ktoś wejdzie. `false` = bot siedzi na kanale cały czas, w ciszy, gdy nikt nie słucha |
| `STREAM_IPV4` | nie | `true` (domyślnie) = przy strumieniu `http://` bot sam ustala adres IPv4 hosta i podaje ffmpeg adres (z oryginalnym nagłówkiem `Host`). Omija to wielosekundowe timeouty DNS dla IPv6 przy nazwach kontenerów Dockera. `false` = ffmpeg sam rozwiązuje nazwę. Strumieni HTTPS bot nigdy nie przepisuje |
| `VOLUME` | nie | Mnożnik głośności, domyślnie `1.0` |
| `LOG_LEVEL` | nie | `DEBUG` / `INFO` / `WARNING` / `ERROR`, domyślnie `INFO` |
| `FFMPEG_BEFORE_OPTIONS` | nie | Nadpisuje opcje ffmpeg przed wejściem (domyślnie: auto-reconnect strumienia) |
| `HEALTH_FILE` | nie | Plik znacznika dla healthchecka, domyślnie `/tmp/streambot.healthy` |

`LEAVE_WHEN_EMPTY` i `STREAM_IPV4` przyjmują też `1`/`0`, `yes`/`no`, `on`/`off`.

Kontener nie wystawia żadnych portów. Musi tylko widzieć `STREAM_URL` (ta sama sieć Dockera albo adres hosta z portem).

### Uprawnienia bota w Discordzie

Scope w OAuth2: `bot` (+ `applications.commands`, jeśli ten sam token używa wtyczka DeadAir).

Uprawnienia do samego streamingu: **View Channels**, **Connect**, **Speak** — suma `3146752`. Jeśli ten sam token obsługuje też tekstową wtyczkę DeadAir, dodaj **Send Messages** i **Read Message History** — suma `3214336`.

```
https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot%20applications.commands&permissions=3214336
```

Bot głosowy nie potrzebuje żadnych Privileged Gateway Intents. Message Content Intent jest potrzebny tylko wtyczce tekstowej, jeśli z niej korzystasz.

### Uruchomienie: Docker Compose

```yaml
services:
  discord-stream-bot:
    image: ghcr.io/radzupl/discord-stream-bot:latest
    container_name: discord-stream-bot
    restart: unless-stopped
    environment:
      DISCORD_TOKEN: ${DISCORD_TOKEN:?}
      VOICE_CHANNEL_ID: "123456789012345678"
      STREAM_URL: http://deadair/live.mp3
      LEAVE_WHEN_EMPTY: "true"
```

### Uruchomienie: Unraid

W `unraid/discord-stream-bot.xml` jest szablon kontenera. Dzięki niemu zmienne są w formularzu z opisami, token jest maskowany, a kontener ma ikonę. Szablon ma domyślnie sieć `docker_network` i ikonę radia — dostosuj do siebie w formularzu albo w pliku.

Unraid trzyma szablony użytkownika jako pliki `my-<nazwa>.xml` w `/boot/config/plugins/dockerMan/templates-user/`. Wystarczy pobrać tam plik z repo.

#### Nowa instalacja

1. Na Unraidzie (Terminal albo SSH) pobierz szablon:

   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml \
     https://raw.githubusercontent.com/RadzuPL/discord-stream-bot/main/unraid/discord-stream-bot.xml
   ```

2. Zakładka **Docker** → **Add Container** → z listy **Template** wybierz `my-discord-stream-bot` (sekcja *User templates*).
3. Wpisz trzy pola: token, ID kanału głosowego, adres strumienia. W razie potrzeby zmień sieć (i ewentualnie ustaw stały IP).
4. **Apply**. W logu kontenera po kilku sekundach pojawi się linia o trybie bezczynności. Przy `LEAVE_WHEN_EMPTY=true` bot wchodzi na kanał (`Joining #<kanał>`) dopiero, gdy ktoś na nim jest.

#### Podmiana szablonu przy już działającym kontenerze

Jeśli kontener już stoi (np. zrobiony ręcznie), nie musisz go usuwać. Unraid dopasowuje kontener do szablonu po nazwie, a ręcznie zrobiony kontener `discord-stream-bot` ma własny plik `my-discord-stream-bot.xml`:

1. Zrób kopię starego szablonu:

   ```bash
   cp /boot/config/plugins/dockerMan/templates-user/my-discord-stream-bot.xml /boot/config/my-discord-stream-bot.xml.bak
   ```

2. Nadpisz go wersją z repo (ta sama komenda `wget -O ...` co wyżej).
3. **Docker** → kliknij kontener → **Edit**. Sprawdź pola: token, ID kanału, adres strumienia, sieć i IP. Jeśli formularz wczytał puste wartości albo domyślne, uzupełnij je z kopii zapasowej (`.bak`) lub z poprzedniego `docker run`.
4. **Apply** przebuduje kontener z nowymi wartościami.

Gdyby formularz zachowywał się dziwnie: usuń sam kontener (obraz zostaje), przywróć `.bak` albo dodaj kontener od nowa według sekcji *Nowa instalacja*.

#### Aktualizacja szablonu

Gdy szablon w repo się zmieni, powtórz `wget -O ...`. Aktualizacje samego obrazu Unraid pokazuje normalnie w zakładce Docker (*update ready*). Nowe opcjonalne zmienne (jak `IDLE_TIMEOUT` czy `LEAVE_WHEN_EMPTY`) działają z wartościami domyślnymi, nawet jeśli twój kontener ich nie definiuje — czyli po aktualizacji obrazu bot wychodzi z pustego kanału, chyba że ustawisz `LEAVE_WHEN_EMPTY=false`.

### Jak to działa

- **Tryb bezczynności (domyślnie).** Strumień gra tylko wtedy, gdy na kanale jest przynajmniej jedna osoba, która faktycznie może go usłyszeć (nie bot, nie osoba z wyłączonym dźwiękiem). Gdy kanał się opróżni, strumień gra jeszcze przez `IDLE_TIMEOUT` sekund (domyślnie 60), na wypadek gdyby ktoś wrócił, potem się zatrzymuje. Pusty kanał to brak ruchu strumienia, a stacja nie widzi słuchacza, więc stacja ustawiona na nadawanie tylko przy słuchaczach (np. ustawienie Playout w DeadAir) też może zasnąć. Jeśli stacja śpi, pierwsze sekundy po wejściu kogoś mogą być ciche, gdy się budzi. `IDLE_TIMEOUT=0` wraca do grania bez przerwy.
- **Wychodzenie z kanału (`LEAVE_WHEN_EMPTY=true`, domyślnie).** Razem z zatrzymaniem strumienia bot rozłącza się z kanałem głosowym, więc Discord przestaje pokazywać na serwerze trwającą rozmowę. Nadal obserwuje kanał (zdarzenia głosowe docierają do niego bez połączenia) i wraca, gdy tylko ktoś wejdzie — zwykle w ciągu kilku sekund; tuż po wyjściu czeka do 5 s przed ponownym wejściem, żeby nie wpaść w pętlę reconnectu discord.py. Przy `LEAVE_WHEN_EMPTY=false` bot siedzi na kanale cały czas i po prostu milczy, gdy nikt nie słucha.
- **Szybki start.** Gdy bot ma wejść na kanał, najpierw otwiera strumień, a na kanał głosowy wchodzi, gdy strumień już się łączy i buforuje (do 5 s zapasu). Dzięki temu handshake głosowy (kilka sekund z DAVE) i start strumienia nakładają się zamiast sumować. Log pokazuje rzeczywiste czasy: `Opening stream from …`, potem `Stream is delivering audio X s after opening it` (połączenie i ffmpeg) oraz `Stream became audible X s after opening it` (pierwszy dźwięk inny niż cisza — stacja w trybie „audience” wysyła ciszę, dopóki nie wróci na antenę). Jeśli druga liczba jest dużo większa od pierwszej, opóźnienie to budzenie stacji, nie bot.
- Pętla nadzorcza co 5 s sprawdza, czy bot jest tam, gdzie powinien (na kanale, gdy ktoś słucha; przy `LEAVE_WHEN_EMPTY=false` zawsze) i czy odtwarzanie pasuje do sytuacji. Jeśli nie — wraca / łączy się ponownie / uruchamia strumień od nowa.
- Padnięty strumień jest ponawiany z rosnącym opóźnieniem (2, 4, 8 … do 60 s), żeby nie zaśmiecać logów ani nie męczyć serwera.
- Ręczne rozłączenie lub przeniesienie bota na inny kanał kończy się powrotem na kanał docelowy — od razu przy `LEAVE_WHEN_EMPTY=false`, a przy `LEAVE_WHEN_EMPTY=true` wtedy, gdy ktoś tam słucha.
- Healthcheck jest zielony, gdy bot albo gra słuchaczom, albo bezczynność jest zamierzona (nie ma dla kogo grać — milczy na kanale albo, przy `LEAVE_WHEN_EMPTY=true`, jest poza nim). Nieosiągalny strumień przy obecnym słuchaczu = `unhealthy`.
- Discord od 1 marca 2026 wymaga szyfrowania E2EE (DAVE) na kanałach głosowych. Obsługuje je discord.py ≥ 2.7 z pakietem `davey`; oba są w `requirements.txt`. Jeśli w logu startowym widzisz `davey MISSING`, głos nie zadziała.
- Kanały typu Stage nie są obsługiwane, tylko zwykłe kanały głosowe.

### Rozwiązywanie problemów

| Objaw | Co sprawdzić |
|---|---|
| `Missing required environment variable` | Brakuje `DISCORD_TOKEN`, `VOICE_CHANNEL_ID` albo `STREAM_URL` |
| `LEAVE_WHEN_EMPTY must be true or false` | Literówka w wartości; użyj `true` albo `false` |
| `Forbidden` / `Missing Access` przy starcie | Bot nie ma View Channels / Connect na tym kanale |
| Bota nie ma na kanale | Przy `LEAVE_WHEN_EMPTY=true` to norma, gdy nikogo więcej nie ma (osoba z wyłączonym dźwiękiem się nie liczy). Wejdzie, gdy ktoś się pojawi. `LEAVE_WHEN_EMPTY=false` zostawia go na stałe |
| Bot siedzi na kanale i milczy | Przy `LEAVE_WHEN_EMPTY=false` to norma, gdy nikogo więcej nie ma (osoba z wyłączonym dźwiękiem się nie liczy). Poza tym: uprawnienie Speak, linia `davey` w logu, czy `STREAM_URL` jest osiągalny z kontenera |
| `Stream ended after 0s` w kółko | Zły adres lub strumień niedostępny; ustaw `LOG_LEVEL=DEBUG`, żeby zobaczyć ffmpeg |
| Muzyka rusza wiele sekund po wejściu bota | Porównaj dwie linie z czasami w logu. Późne `delivering audio` = połączenie/DNS (szukaj też `IPv4 lookup for the stream host took …`); `became audible` dużo później niż `delivering` = stacja była poza anteną i się rozgrzewa |

### Rozwój

Push do `main` buduje i publikuje `latest` oraz tag `sha-<krótki hash>`; tag `v1.2.3` publikuje też `1.2.3`. Raz w tygodniu (poniedziałek) obraz przebudowuje się sam, żeby łapać świeże zależności.

### Licencja

MIT — zobacz [LICENSE](LICENSE). Obraz kontenera zawiera też oprogramowanie osób trzecich (Python, ffmpeg, discord.py, davey i inne), każde na własnej licencji.
