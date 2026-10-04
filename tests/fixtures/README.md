# Generated test media

No downloaded/archive media belongs in this directory. The MP4 used by the browser tests is a synthetic solid-color clip with a sine-wave audio track so muted playback can be tested.

After installing the project dependencies and FFmpeg, generate it locally:

```text
npm run fixtures
```

`npm test` also generates it if missing. The generator leaves an existing fixture unchanged. Generated MP4 files are ignored by Git and are not included in widget packages.
