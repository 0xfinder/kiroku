# League API recording

Recorded 2026-10-12 from the free Data Dragon and lolesports HTTP APIs. The reference client is `0xfinder/live-lol-esports/src/utils/LoLEsportsAPI.ts`.

- `getLive.json` is the real empty response.
- `getSchedule.json` is the real 80-event schedule response.
- `getEventDetails.json` is RED Kalunga vs FlyQuest, 2026-10-08, match `117133830114188936`.
- `window.json` and `details.json` are game 1, `117133830114188937`, from the feed without a `startingTime` parameter. Only the first frame is retained. Picks and players are the unmodified `gameMetadata` from the window response. Details is retained as evidence of the companion endpoint's schema; picks require only window metadata.
- `getTeams.json` retains the two match teams from the real 1,586-team response, including all their registered players and their original status.
- `versions.json` retains the current version, `16.20.1`. `champion.json` and `item.json` retain every catalogue entry's ID and name and the response envelope. Combat stats and artwork URLs are omitted.

No pro match was live during recording. `LeagueReplay` selects this completed event from the schedule and changes only its event state and game 1 state to `inProgress`. The committed files keep the original states. This proves routing and Gate storage with real teams and picks; it does not prove the live feed clock during a running match.

These responses contain no bans. The summary uses `bans: null`, which means unavailable, not an empty ban list. Data Dragon also includes items whose name is empty. They are internal entries and are excluded from Vocabulary.
