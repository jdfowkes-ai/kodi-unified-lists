import sys
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode, parse_qs
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs
from media_display import MediaDisplay

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE = sys.argv[0]
PARAMS = {k: v[0] for k, v in parse_qs(sys.argv[2][1:]).items()}
DB_PATH = Path(xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))) / 'lists.db'
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DISPLAY = MediaDisplay()


def db():
    con = sqlite3.connect(str(DB_PATH))
    con.execute('CREATE TABLE IF NOT EXISTS entries (list_name TEXT NOT NULL, media_type TEXT NOT NULL, kodi_id INTEGER NOT NULL, title TEXT NOT NULL, PRIMARY KEY (list_name, media_type, kodi_id))')
    con.execute("CREATE TABLE IF NOT EXISTS playback_progress (media_type TEXT NOT NULL, kodi_id INTEGER NOT NULL, title TEXT NOT NULL, file_path TEXT NOT NULL, position REAL NOT NULL, duration REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, source_metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(media_type,kodi_id))")
    columns = {row[1] for row in con.execute('PRAGMA table_info(playback_progress)')}
    if 'source_metadata' not in columns:
        con.execute("ALTER TABLE playback_progress ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}'")
    con.execute('CREATE TABLE IF NOT EXISTS external_list_items (media_type TEXT NOT NULL, kodi_id INTEGER NOT NULL, path TEXT NOT NULL, is_folder INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT \'{}\', PRIMARY KEY(media_type,kodi_id))')
    con.execute('CREATE TABLE IF NOT EXISTS user_lists (list_name TEXT PRIMARY KEY COLLATE NOCASE)')
    con.commit()
    return con


def url(**kwargs):
    return BASE + '?' + urlencode(kwargs)


def item(label, target, folder=True, media=None, mid=None, progress=None):
    li = DISPLAY.listitem(media, mid, label, progress) if media else xbmcgui.ListItem(label=label)
    xbmcplugin.addDirectoryItem(HANDLE, target, li, folder)


def movie_progress(mid):
    con = db()
    try:
        row = con.execute('SELECT position,duration FROM playback_progress WHERE media_type=? AND kodi_id=? AND completed=0 AND position>=30', ('movie', int(mid))).fetchone()
        return row
    finally:
        con.close()


def show_list(name):
    xbmcplugin.setContent(HANDLE, "videos")
    con = db()
    rows = con.execute('SELECT e.media_type,e.kodi_id,e.title,x.path,x.is_folder,x.metadata_json '
                        'FROM entries e LEFT JOIN external_list_items x '
                        'ON x.media_type=e.media_type AND x.kodi_id=e.kodi_id '
                        'WHERE e.list_name=? ORDER BY e.title', (name,)).fetchall()
    for media, mid, title, path, is_folder, metadata_json in rows:
        if path:
            try:
                metadata = json.loads(metadata_json or '{}')
            except (ValueError, TypeError):
                metadata = {}
            if isinstance(metadata, dict):
                DISPLAY.cache[(media, int(mid))] = metadata
            saved = con.execute('SELECT position,duration FROM playback_progress '
                                'WHERE media_type=? AND kodi_id=? AND completed=0 AND position>=30',
                                (media, int(mid))).fetchone()
            if name == 'Watch Later' and media in ('movie', 'episode') and not is_folder:
                import watch_later_actions as watch_later_view
                data = watch_later_view.watch_later_payload(media, mid, title, path,
                                                          metadata_json, saved)
                action_url = watch_later_view._addon_url('watch_later_actions', data)
                li = watch_later_view._listitem(data)
                li.addContextMenuItems([
                    ('Remove from ' + name, 'RunPlugin(%s)' % url(action='remove', list=name, media=media, mid=mid)),
                    ('Add to another list', 'RunPlugin(%s)' % url(action='choose_list', media=media, mid=mid, title=title))
                ])
                xbmcplugin.addDirectoryItem(HANDLE, action_url, li, False)
                continue
            # Watch Later may contain a show-level Umbrella folder as well as
            # individual movies/episodes. Enrich show folders too so their
            # info panel receives TV genres, UK certification, dates and art.
            li = DISPLAY.listitem(
                media, mid, title, saved,
                enrich=(name == 'Watch Later' and media == 'tvshow'))
            if saved and saved[1] > 0:
                li.setProperty('WatchedProgress', str(min(100, max(0, round(100 * saved[0] / saved[1])))))
            if not is_folder:
                li.setProperty('IsPlayable', 'true')
            li.addContextMenuItems([
                ('Remove from ' + name, 'RunPlugin(%s)' % url(action='remove', list=name, media=media, mid=mid)),
                ('Add to another list', 'RunPlugin(%s)' % url(action='choose_list', media=media, mid=mid, title=title))
            ])
            xbmcplugin.addDirectoryItem(HANDLE, path, li, bool(is_folder))
            continue
        item(title, url(action='unavailable'), False, media=media, mid=mid)
    con.close()


def change(add):
    name, media, mid = PARAMS.get('list', ''), PARAMS.get('media', ''), PARAMS.get('mid', '')
    if not is_known_list(name) or media not in ('movie', 'tvshow', 'episode') or not mid.isdigit():
        return
    con = db()
    if add:
        con.execute('INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)', (name, media, int(mid), PARAMS.get('title', 'Untitled')))
    else:
        con.execute('DELETE FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?', (name, media, int(mid)))
        con.execute('DELETE FROM external_list_items WHERE media_type=? AND kodi_id=? AND NOT EXISTS (SELECT 1 FROM entries WHERE media_type=? AND kodi_id=?)', (media, int(mid), media, int(mid)))
    con.commit()
    con.close()
    xbmcgui.Dialog().notification('Unified Lists', 'List updated', xbmcgui.NOTIFICATION_INFO, 2000)
    xbmc.executebuiltin('Container.Refresh')


def list_names(include_defaults=True):
    names = ['Watch Later', 'Favourites'] if include_defaults else []
    con = db()
    try:
        names.extend(row[0] for row in con.execute('SELECT list_name FROM user_lists ORDER BY list_name COLLATE NOCASE'))
    finally:
        con.close()
    return names


def is_known_list(name):
    return name in list_names()


def manage_lists():
    dialog = xbmcgui.Dialog()
    choice = dialog.select('My Lists', ['Create a list', 'Delete a list', 'Cancel'])
    if choice == 0:
        name = dialog.input('New list name', type=xbmcgui.INPUT_ALPHANUM)
        name = (name or '').strip()
        if not name or len(name) > 40 or name.casefold() in {x.casefold() for x in list_names()}:
            if name:
                dialog.notification('Unified Lists', 'Use a unique name up to 40 characters', xbmcgui.NOTIFICATION_WARNING, 2500)
            return
        con = db()
        con.execute('INSERT INTO user_lists(list_name) VALUES (?)', (name,))
        con.commit()
        con.close()
        xbmc.executebuiltin('Container.Refresh')
    elif choice == 1:
        custom = list_names(include_defaults=False)
        if not custom:
            dialog.notification('Unified Lists', 'There are no custom lists to delete', xbmcgui.NOTIFICATION_INFO, 2200)
            return
        selected = dialog.select('Delete list', custom)
        if selected < 0:
            return
        name = custom[selected]
        if dialog.yesno('Delete list', 'Delete "%s" and its saved entries?' % name):
            con = db()
            rows = con.execute('SELECT media_type,kodi_id FROM entries WHERE list_name=?', (name,)).fetchall()
            con.execute('DELETE FROM entries WHERE list_name=?', (name,))
            for media, mid in rows:
                con.execute('DELETE FROM external_list_items WHERE media_type=? AND kodi_id=? AND NOT EXISTS (SELECT 1 FROM entries WHERE media_type=? AND kodi_id=?)', (media, mid, media, mid))
            con.execute('DELETE FROM user_lists WHERE list_name=?', (name,))
            con.commit()
            con.close()
            xbmc.executebuiltin('Container.Refresh')


def choose_list():
    names = list_names()
    choice = xbmcgui.Dialog().select('Add to list', names)
    if choice < 0:
        return
    name = names[choice]
    PARAMS['list'] = name
    change(True)


def toggle_saved_progress():
    name, media, raw_mid = PARAMS.get('list', ''), PARAMS.get('media', ''), PARAMS.get('mid', '')
    if name not in ('Watch Later', 'Favourites') or media not in ('movie', 'episode'):
        return
    try:
        mid = int(raw_mid)
    except (TypeError, ValueError):
        return
    con = db()
    con.row_factory = sqlite3.Row
    try:
        row = con.execute('SELECT * FROM playback_progress WHERE media_type=? AND kodi_id=?',
                          (media, mid)).fetchone()
        exists = con.execute('SELECT 1 FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?',
                             (name, media, mid)).fetchone()
        if row is None:
            if not exists:
                return
            con.execute('DELETE FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?',
                        (name, media, mid))
            con.execute('DELETE FROM external_list_items WHERE media_type=? AND kodi_id=? '
                        'AND NOT EXISTS (SELECT 1 FROM entries WHERE media_type=? AND kodi_id=?)',
                        (media, mid, media, mid))
            con.commit()
            xbmcgui.Dialog().notification('Unified Lists', 'Removed from ' + name,
                                          xbmcgui.NOTIFICATION_INFO, 2000)
            return
        if exists:
            con.execute('DELETE FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?',
                        (name, media, mid))
        else:
            metadata_json = row['source_metadata'] or '{}'
            try:
                metadata = json.loads(metadata_json)
            except (ValueError, TypeError):
                metadata = {}
            from umbrella_playback import build as umbrella_play_url
            play_path = umbrella_play_url(metadata, row['file_path'])
            con.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?)',
                        (name, media, mid, row['title']))
            con.execute('INSERT OR REPLACE INTO external_list_items '
                        '(media_type,kodi_id,path,is_folder,metadata_json) VALUES (?,?,?,?,?)',
                        (media, mid, play_path, 0, metadata_json))
        con.commit()
    finally:
        con.close()
    xbmcgui.Dialog().notification('Unified Lists', 'List updated', xbmcgui.NOTIFICATION_INFO, 2000)


def play_external_item():
    media, raw_mid = PARAMS.get('media', ''), PARAMS.get('mid', '')
    if media not in ('movie', 'episode'):
        return
    try:
        mid = int(raw_mid)
    except (TypeError, ValueError):
        return
    con = db()
    row = con.execute('SELECT x.path,x.metadata_json,p.position,p.completed FROM external_list_items x '
                      'LEFT JOIN playback_progress p ON p.media_type=x.media_type AND p.kodi_id=x.kodi_id '
                      'WHERE x.media_type=? AND x.kodi_id=?', (media, mid)).fetchone()
    con.close()
    if not row or not row[0]:
        return
    path, metadata_json, saved_position, completed = row
    try:
        metadata = json.loads(metadata_json or '{}')
    except (ValueError, TypeError):
        metadata = {}
    from umbrella_playback import build as umbrella_play_url
    path = umbrella_play_url(metadata, path)
    position = float(saved_position or 0) if saved_position is not None and not completed else 0.0
    if PARAMS.get('start') == '0':
        position = 0.0
        import umbrella_local
        umbrella_local.clear_bookmark(media, metadata)
    from resume_control import queue
    queue(DB_PATH, media, mid, position, path, metadata)
    from strm_playback import create as create_strm
    xbmc.Player().play(create_strm(media, mid, path))


def play_new_item():
    try:
        payload = json.loads(PARAMS.get('data', '{}'))
    except (ValueError, TypeError):
        return
    metadata = payload.get('metadata') if isinstance(payload, dict) else {}
    if not isinstance(metadata, dict):
        return
    from umbrella_playback import build as umbrella_play_url
    path = umbrella_play_url(metadata, payload.get('umbrella_path', ''))
    from identity import stable_identity
    mid, _identity = stable_identity('episode', metadata, path)
    from strm_playback import create as create_strm
    xbmc.Player().play(create_strm('episode', mid, path))


def save_metadata_item():
    name = PARAMS.get('list', '')
    if name not in ('Watch Later', 'Favourites'):
        return
    try:
        payload = json.loads(PARAMS.get('data', '{}'))
    except (ValueError, TypeError):
        return
    metadata = payload.get('metadata') if isinstance(payload, dict) else {}
    if not isinstance(metadata, dict):
        return
    from identity import stable_identity
    media, path = 'episode', payload.get('umbrella_path', '')
    mid, _identity = stable_identity(media, metadata, path)
    title = metadata.get('title') or metadata.get('tvshowtitle') or 'Untitled'
    con = db()
    con.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?)', (name, media, mid, title))
    con.execute('INSERT OR REPLACE INTO external_list_items '
                '(media_type,kodi_id,path,is_folder,metadata_json) VALUES (?,?,?,?,?)',
                (media, mid, path, 0, json.dumps(metadata, ensure_ascii=False)))
    con.commit()
    con.close()
    xbmcgui.Dialog().notification('Unified Lists', 'Added to ' + name, xbmcgui.NOTIFICATION_INFO, 2000)

def umbrella_group(items):
    """Expose Umbrella-owned folders so Umbrella keeps native playback/menu behavior."""
    xbmcplugin.setContent(HANDLE, 'videos')
    for label, target in items:
        item(label, target, True)



def umbrella_favourites():
    umbrella_group([
        ('Movies', 'plugin://plugin.video.umbrella/?action=getFavouritesMovies&url=favourites_movies'),
        ('TV Shows', 'plugin://plugin.video.umbrella/?action=getFavouritesTVShows&url=favourites_tvshows'),
        ('Episodes', 'plugin://plugin.video.umbrella/?action=getFavouritesEpisodes'),
    ])


def umbrella_widgets():
    """Expose Umbrella-owned progress and episode lists for home widgets."""
    umbrella_group([
        ('Continue Movies', 'plugin://plugin.video.umbrella/?action=moviesUnfinished'),
        ('Continue TV Episodes', 'plugin://plugin.video.umbrella/?action=episodesUnfinished'),
        ('TV Series Progress / Next Episode', 'plugin://plugin.video.umbrella/?action=shows_progress'),
        ('Up Next Episodes', 'plugin://plugin.video.umbrella/?action=upcomingProgress'),
        ('Finished Movies', 'plugin://plugin.video.umbrella/?action=local_finish_watching_movies'),
        ('Finished TV Episodes', 'plugin://plugin.video.umbrella/?action=local_finish_watching_episodes'),
        ('Finished TV Shows', 'plugin://plugin.video.umbrella/?action=shows_watched'),
    ])


def main():
    action = PARAMS.get('action', '')
    if action in ('certificate_icons', 'certificate_restore'):
        from certificate_icons import run
        run(ADDON, restore=action == 'certificate_restore')
    elif action == 'settings':
        ADDON.openSettings()
    elif action == 'manage_lists':
        manage_lists()
    elif action == 'choose_list':
        choose_list()
    elif action in ('apps', 'manage_apps', 'launch_app', 'add_app',
                    'choose_remove_app', 'remove_app', 'restore_apps', 'filter_apps',
                    'choose_spotlight_source', 'spotlight_source_browser', 'set_spotlight_source'):
        import app_manager
        import xbmcvfs
        addon_root = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
        app_manager.run_action(ADDON, DB_PATH, addon_root, BASE, PARAMS)
        if action in ('apps', 'manage_apps', 'choose_spotlight_source', 'spotlight_source_browser'):
            return
    elif action == 'app_spotlight':
        import app_manager
        import app_spotlight
        import xbmcvfs
        addon_root = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
        apps = app_manager.list_apps(DB_PATH, addon_root)
        app = next((entry for entry in apps if entry['id'] == PARAMS.get('app', '')), None)
        if app is None:
            xbmcgui.Dialog().notification('App spotlight', 'App is not in the active app list.',
                                          xbmcgui.NOTIFICATION_WARNING, 3000)
        elif action == 'app_spotlight':
            app_spotlight.render_spotlight(ADDON, HANDLE, BASE, addon_root, app)
            return
    elif action == 'icon_library':
        import icon_library
        icon_library.run(ADDON, HANDLE, BASE, PARAMS)
        return
    elif action == 'watch_later_actions':
        import watch_later_actions as watch_later_view
        watch_later_view.run_action()
    elif action.startswith('watch_later_'):
        import watch_later_actions as watch_later_view
        watch_later_view.run_action()
    elif action == 'favourites':
        umbrella_favourites()
    elif action == 'umbrella_widgets':
        umbrella_widgets()
    elif action == 'play_external':
        play_external_item()
    elif action == 'play_new':
        play_new_item()
    elif action == 'save_metadata':
        save_metadata_item()
    elif action == 'toggle_saved':
        toggle_saved_progress()
    elif action == 'unavailable':
        xbmcgui.Dialog().notification('Unified Lists', 'This saved entry needs its Umbrella item link again',
                                      xbmcgui.NOTIFICATION_WARNING, 4000)
    elif action == 'list':
        show_list(PARAMS.get('list', ''))
    elif action == 'tmdbhelper':
        xbmc.executebuiltin('Container.Update(%s)' % PARAMS.get('path', 'plugin://plugin.video.umbrella/'))
    elif action in ('add', 'remove'):
        change(action == 'add')
    elif action == 'details':
        xbmc.executebuiltin('Action(Info)')
    else:
        item('Settings', url(action='settings'), False)
        item('Apps', url(action='apps'))
        item('Manage Apps', url(action='manage_apps'))
        item('Icon Library', url(action='icon_library'))
        item('Watch Later', url(action='list', list='Watch Later'))
        item('Favourites', url(action='list', list='Favourites'))
        item('Umbrella Favourites', url(action='favourites'))
        item('Umbrella Widgets', url(action='umbrella_widgets'))
        for list_name in list_names(include_defaults=False):
            item(list_name, url(action='list', list=list_name))
        item('My Lists', url(action='manage_lists'))
    xbmcplugin.endOfDirectory(HANDLE)


if __name__ == '__main__':
    main()
