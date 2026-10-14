# Dota HTTP fixtures

Recorded 2026-10-14 for issue #28. Tests use MockTransport and never call live APIs. Team recency is evaluated at 2026-10-14T00:00:00Z during replay, without freezing the cache or watcher clock.

- `heroes.json` and `items.json`: projections of [dotaconstants heroes](https://raw.githubusercontent.com/odota/dotaconstants/master/build/heroes.json) and [items](https://raw.githubusercontent.com/odota/dotaconstants/master/build/items.json). Only consumed name fields remain; unnamed internal items retain `{}` so their absence is tested. The current live response has no whitespace-only item; `items.json` adds a clearly named `__whitespace_regression__` sentinel with `dname: " "` to retain coverage of that reported failure.
- `teams.json`: ID/name/tag/last-match-time projection of [OpenDota teams](https://api.opendota.com/api/teams), of all 1,000 returned rows, including 16 empty names, one whitespace-only name, and duplicate OG tags plus RE/MMR/alt. Tests skip the 17 invalid names and match against all 983 valid rows.
- `players-7119388.json`, `players-9823272.json`: complete recorded team-player arrays. OpenDota marks six Yandex players current, including DM and AMMAR_THE_F; this is a roster, not an exact match lineup. Historic rows can have null names and null membership.

The spike's `sources/` retains these same shapes and the alias research. No field has been changed to invent a current lineup.
