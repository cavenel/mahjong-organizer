# Screenshots

The images referenced by [../guide.md](../guide.md), under these exact filenames
so the `![...](screenshots/<file>)` links resolve.

Suggested capture size: a normal desktop browser window (the console is
responsive); for screen/preview shots a 1920×1080 display view is ideal.

> 💡 **Tip:** capture on the **test tenant** (`https://test.<your-domain>/`) so
> nothing touches a real event — sign in as an **admin**, run **Import from
> template**, then **Fill all rounds — scores / — score sheets** so every page
> has realistic data. Where a shot needs a specific role, sign in with an
> account holding that role.

---

## Still to capture or recapture

The console has evolved since some shots were taken. These show a UI that no
longer matches the guide's text and should be redone (same filenames):

### `32-publisher-overview.png` — Publisher overview page *(never captured)*
- **Page:** `admin?page=publisher_overview` · guide [§11](../guide.md#11-publisher-overview)
- **Steps:** sign in as **publisher** (or admin), make sure the tournament has
  data and a few published rounds, capture the one-row-per-round table with its
  progress counts and *Published* toggles.

### `02-assign-role.png` — assigning roles *(shows the old Django-admin flow)*
- **Now:** Administration → **User management** (`admin?page=users`) — capture a
  user row with its role checkboxes. Guide
  [§16](../guide.md#16-user-management).

### `03-sidebar-staff.png` / `04-sidebar-scorer.png` — the sidebar *(sections have changed)*
- **Now:** Dashboard / Configuration / Players / Scoring / Displays / Results /
  Administration (admin), and Scoring alone (scorer). Guide
  [§2](../guide.md#2-getting-around-the-console).

### `25-display-settings.png` — display settings *(fields have changed)*
- **Now:** zoom, score lines, columns (totals view), announcement message, page
  rotation time. Guide [§12](../guide.md#12-driving-the-screens).
