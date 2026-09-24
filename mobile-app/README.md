# LedgerBooks Mobile (Capacitor wrapper)

This is **not** a rebuilt app — it's a thin native shell (via [Capacitor](https://capacitorjs.com))
that installs LedgerBooks (the existing mobile-friendly Flask web app) as a real app icon on a
phone's home screen, launchable from the App Store / Play Store, rather than a browser tab.

## What's done

- `npm install` — Capacitor core/CLI/Android/iOS packages installed.
- `npx cap add android` — a full native Android project scaffolded under `android/`.
- `capacitor.config.json` — configured to load a live URL directly (`server.url`), not a bundled
  copy of the site. That means logins, new features, everything works exactly like the website —
  there's no separate app build to ship every time the web app changes.

## What's left before this is a real, installable app

1. **Deploy LedgerBooks somewhere with a real HTTPS URL.** `capacitor.config.json`'s `server.url`
   is currently a placeholder (`https://your-ledgerbooks-domain.example`) — it can't point at
   `localhost`, since that only exists on this dev machine. Update that one line once the app has
   a real deployed address (see `render.yaml` in the project root — Render was already set up as
   the deploy target).

2. **Install Android Studio** (this machine doesn't have it — `adb`/`android` weren't found).
   Once installed:
   ```
   cd mobile-app
   npm run android      # opens android/ in Android Studio
   ```
   From there, Android Studio builds and runs the APK on an emulator or a plugged-in phone,
   and eventually produces the signed release bundle Play Store submission needs.

3. **For iOS**: needs a Mac with Xcode — `npx cap add ios` (not run here, since it needs macOS
   tooling this Windows machine doesn't have) followed by the same "open in Xcode, build" flow.

4. **App Store accounts**, if you want it publicly listed:
   - Google Play: one-time $25 registration.
   - Apple Developer Program: $99/year.
   (Skip both if this is just for internal/company-phone installs — Android in particular can
   sideload a signed APK without ever going through Play Store.)

5. **App icon / splash screen** — Capacitor ships generic placeholders; swap them in
   `android/app/src/main/res/` (and the iOS equivalent, once that platform exists) for
   LedgerBooks' own branding before shipping.

Nothing above needs the Flask app itself touched again — this wrapper reads the live site as-is.
