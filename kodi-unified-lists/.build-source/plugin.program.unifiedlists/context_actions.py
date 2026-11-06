"""Choose a Unified Lists destination for an Umbrella item."""
import json
import sqlite3
import sys
from pathlib import Path

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from identity import stable_identity

ADDON = xbmcaddon.Addon()
PROFILE = Path(xbmcvfs.translatePath(ADDON.getAddonInfo('profile')))
DB_PATH = PROFILE / 'lists.db'


def call(obj, name, default='', *args):
    try:
        value = getattr(obj, name)(*args)
        return value if value not in (None, '') else default
    except Exception:
        return default


def info(label):
    try:
        return xbmc.getInfoLabel(label) or ''
    except Exception:
        return ''


def selected_item():
    li = getattr(sys, 'listitem', None)
    if li is None:
        return None
    path = call(li, 'getPath') or info('ListItem.FileNameAndPath')
    if not path.startswith('plugin://plugin.video.umbrella/'):
        return None
    tag = call(li, 'getVideoInfoTag', None)
    title = call(tag, 'getTitle') or info('ListItem.Title') or call(li, 'getLabel') or info('ListItem.Label')
    showtitle = call(tag, 'getTvShowTitle') or info('ListItem.TVShowTitle')
    media = str(call(tag, 'getMediaType') or info('ListItem.DBTYPE')).lower()
    if media not in ('movie', 'tvshow', 'episode'):
        media = 'episode' if showtitle else 'movie'
    try:
        art = li.getArt() or {}
    except Exception:
        art = {}
    if not isinstance(art, dict):
        art = {}
    # Some providers expose episode art only through the parent-show keys or
    # skin info labels. Preserve those values so Watch Later can render the
    # series poster even when the episode itself has no thumbnail.
    for key in ('poster', 'thumb', 'fanart', 'landscape', 'banner', 'clearlogo'):
        parent_art = info('ListItem.Art(tvshow.%s)' % key)
        if parent_art and not art.get('tvshow.' + key):
            art['tvshow.' + key] = parent_art
    ids = {}
    for key in ('tmdb', 'tvdb', 'imdb'):
        value = call(tag, 'getUniqueID', '', key) or info('ListItem.UniqueID(%s)' % key)
        if value:
            ids[key] = str(value)
    try:
        season = int(call(tag, 'getSeason', 0) or info('ListItem.Season') or 0)
        episode = int(call(tag, 'getEpisode', 0) or info('ListItem.Episode') or 0)
        year = int(call(tag, 'getYear', 0) or info('ListItem.Year') or 0)
    except (TypeError, ValueError):
        season, episode, year = 0, 0, 0
    metadata = {
        'title': title or showtitle or 'Untitled', 'showtitle': showtitle,
        'tvshowtitle': showtitle, 'year': year, 'season': season, 'episode': episode,
        'plot': call(tag, 'getPlot') or info('ListItem.Plot'),
        'uniqueid': ids, 'art': art,
        'thumbnail': art.get('thumb') or art.get('poster') or info('ListItem.Thumb'),
        'fanart': art.get('fanart') or art.get('landscape') or info('ListItem.Art(fanart)'),
        'mpaa': call(tag, 'getMpaa') or info('ListItem.MPAA'),
    }
    try:
        genres = call(tag, 'getGenres', []) or []
        if genres:
            metadata['genres'] = [str(value) for value in genres]
    except (TypeError, AttributeError):
        pass
    mid, identity = stable_identity(media, metadata, path)
    metadata['local_identity'] = identity
    try:
        is_playable = li.getProperty('IsPlayable').lower() == 'true'
    except Exception:
        is_playable = False
    is_folder = media == 'tvshow' and not is_playable
    return media, mid, metadata, path, is_folder


def choose_destination():
    selected = selected_item()
    if selected is None:
        xbmcgui.Dialog().notification('Unified Lists', 'Could not read this Umbrella item', xbmcgui.NOTIFICATION_WARNING, 3000)
        return
    media, mid, metadata, path, is_folder = selected
    PROFILE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS user_lists (list_name TEXT PRIMARY KEY COLLATE NOCASE)')
        names = ['Watch Later', 'Favourites']
        names.extend(row[0] for row in con.execute('SELECT list_name FROM user_lists ORDER BY list_name COLLATE NOCASE'))
    finally:
        con.close()
    choice = xbmcgui.Dialog().select('Add / Remove Unified List', names)
    if choice < 0:
        return
    list_name = names[choice]
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS entries (list_name TEXT NOT NULL, media_type TEXT NOT NULL, kodi_id INTEGER NOT NULL, title TEXT NOT NULL, PRIMARY KEY (list_name, media_type, kodi_id))')
        con.execute("CREATE TABLE IF NOT EXISTS external_list_items (media_type TEXT NOT NULL, kodi_id INTEGER NOT NULL, path TEXT NOT NULL, is_folder INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(media_type,kodi_id))")
        found = con.execute('SELECT 1 FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?', (list_name, media, mid)).fetchone()
        if found:
            con.execute('DELETE FROM entries WHERE list_name=? AND media_type=? AND kodi_id=?', (list_name, media, mid))
            con.execute('DELETE FROM external_list_items WHERE media_type=? AND kodi_id=? AND NOT EXISTS (SELECT 1 FROM entries WHERE media_type=? AND kodi_id=?)', (media, mid, media, mid))
            message = 'Removed from ' + list_name
        else:
            title = metadata.get('title') or metadata.get('tvshowtitle') or 'Untitled'
            con.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?)', (list_name, media, mid, title))
            con.execute('INSERT OR REPLACE INTO external_list_items VALUES (?,?,?,?,?)', (media, mid, path, int(is_folder), json.dumps(metadata, ensure_ascii=False)))
            message = 'Added to ' + list_name
        con.commit()
    finally:
        con.close()
    xbmcgui.Dialog().notification('Unified Lists', message, xbmcgui.NOTIFICATION_INFO, 2500)


if __name__ == '__main__':
    choose_destination()
