# Changelog

Record each release newest first. Bump minor for features and visible behaviour, patch for fixes and polish.

## 0.8.0 - 2026-11-19

- Press Command+F (Ctrl+F on Windows or Linux) to find text in the current desktop window, including chats, inbox items and runner logs. Enter and Shift+Enter move between matches, and Escape closes Find.

## 0.7.1 - 2026-11-19

- An expanded worker on the Workers page is easier to read. It opens with what the worker does and what its last run reported, including when it ran and how long it took, with the run's details folded away. Its settings now sit under a closed **Settings** section, grouped into Schedule, AI and Name. The details the row already shows are no longer repeated.

## 0.7.0 - 2026-11-19

- Turn the Roosta app's notifications on or off under **Settings → Notifications**. While they are off, new board items and sign-in waits still appear on the board but no longer pop up, and turning them back on does not replay what arrived in between. On a Mac, a link opens System Settings for banners, sounds and Focus.

## 0.6.0 - 2026-11-18

- The Workers page lists your workspace’s skills that no worker runs yet, under **Not on a schedule**. Click a skill’s name to read its SKILL.md, or **Schedule** to ask the Assistant to make it a worker; the request is filled in for you to edit before sending.

## 0.5.0 - 2026-11-18

- Workers using named connections can edit Roosta’s source when its checkout is the selected workspace. Connection credentials and managed runtime directories remain protected.

## 0.4.0 - 2026-11-18

- If the saved workspace has moved or been deleted, Roosta now says **Workspace not found** and lets you choose another workspace instead of reporting runner exit code `-2`.
- Workers now follow the connected default without writing a `connection` setting. Choosing a named account is the only action that pins a worker, and the ambiguous previous-setup choices are gone.

## 0.3.1 - 2026-11-18

- **Back to Pulse** on the full-screen constellation now returns to the Pulse page straight away, instead of doing nothing until you had selected a project.

- The Roosta worker now wears the mascot’s orange hood and purple goggles in the Office and crew portraits.

## 0.3.0 - 2026-11-18

- Grant a named worker standing approval to comment on open ready issues and add the in review label in one GitHub repository.

## 0.2.0 - 2026-11-18

- Resize the side-by-side columns on the Assistant, Workspace, Workers and Pulse pages by dragging the divider between them, or by focusing it and using the arrow keys. Double-click the divider or press Enter to restore the default. Each page remembers your widths, including after you restart the app.
- Roosta Engineer now improves your workspace exclusively. It drafts feedback for your review when a change belongs in the app.

## 0.1.3 - 2026-11-17

- The Assistant chat no longer asks permission again and again for workspace changes you asked for, and it points at the approval button instead of asking "Approve?" as a question. Typing "go" while a card waits gets a one-line pointer to the button, not a fresh draft.
- Workspace and Pulse now show only what a standard workspace holds: the People collection, the `people` front-matter attribution, and the constellation's People, Incidents and Notes layers are gone.
- The learner's chat helper dates each Assistant chat by its own latest message, so a scheduled pass reads only chats that really changed instead of every chat each time the history file is saved.
- The Workspace page has an **Open folder** button that opens your workspace in Finder (Explorer on Windows, your file manager on Linux). It works only on the computer running Roosta.

## 0.1.2 - 2026-11-17

- Fix macOS first launch in an existing folder: ask to migrate a compatible workspace setup or adopt the folder, preserving documents and skills.
- Brand the desktop loading screen with the Roosta character instead of generative art, and use it as the dock icon in development.

## 0.1.0 - 2026-11-17

- Initial release of Roosta, your agentic office.
