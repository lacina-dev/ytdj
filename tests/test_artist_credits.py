"""Kdo je uvedený u skladby (hlášení #44: „AC/DC rozkouskoval na AC a DC").

Různí interpreti jen za čárkou, středníkem, feat./ft./featuring a vs.; "&",
"/", "+" a samotné "x" jsou uvnitř jména. Jméno s čárkou, které YouTube Music
uvádí jako jednoho interpreta, drží pohromadě. Staré uložené kousky jmen
(👍 „AC", skladba „ac|thunderstruck") opraví při startu artistfix.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_artist_credits -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-credits-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytdj import aitvorba as A  # noqa: E402
from ytdj import artistfix  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music import match  # noqa: E402
from ytdj.music import repeats  # noqa: E402
from ytdj.music.catalog import Track, to_track  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.votes import ARTIST, SONG, VoteBook  # noqa: E402
from ytdj.web import ai_api  # noqa: E402
from ytdj.web.server import _track_dict  # noqa: E402

# jména z historie Pi, která se dřív rozpadla na "&", "/", "+" nebo "x"
SINGLE = ["AC/DC", "Iron & Wine", "Indy & Wich", "Wendy & Lisa", "Joan Jett & the Blackhearts",
          "Bruce Hornsby & The Range", "Simon & Garfunkel", "K/DA", "The Mamas & The Papas",
          "Bob Marley & The Wailers", "Katrina & The Waves", "Macklemore & Ryan Lewis",
          "Selena Gomez & The Scene", "Art Blakey & The Jazz Messengers", "Xindl X", "Lil Nas X",
          "Static-X", "Tři Sestry/Visaci Zamek", "Olly Alexander (Years & Years)", "Fth Stylin",
          "Featherstone", "Žalman & spol."]
COMMA = ["Earth, Wind & Fire", "Crosby, Stills, Nash & Young", "Tyler, The Creator",
         "Emerson, Lake & Palmer", "Blood, Sweat & Tears"]
# běžná jména: klíče se nesmí změnit (hlasy, vyřazení, neopakování)
NORMAL = [("Kabát", "Malá dáma (Official Video)"), ("Kabát - Topic", "Pohoda [Live]"),
          ("The Beatles", "Let It Be - Remastered 2009"), ("Tomáš Klus, Kabát", "Song"),
          ("Daft Punk, Pharrell Williams, Nile Rodgers", "Get Lucky (Radio Edit)"),
          ("David Stypka, Bandjeez", "Dobré ráno, milá (feat. Ewa Farna)"),
          ("Mňága a Žďorp", "Hodnej švarnej kluk"), ("Olympic", "Jasná zpráva"),
          ("Aleš Zenkl, Josef Čihák, Záviš, Josef Čihák jun.", "Smíření")]


def ytm(vid: str, title: str, *names: str) -> Track:
    """Skladba tak, jak ji vrací YouTube Music: interpreti zvlášť."""
    return to_track({"videoId": vid, "title": title,
                     "artists": [{"name": n, "id": f"UC{i}"} for i, n in enumerate(names)]})


def run(coro):
    return asyncio.run(coro)


class TextOnly(unittest.TestCase):
    def test_names_with_and_slash_plus_x_stay_whole(self):
        for name in SINGLE:
            with self.subTest(name=name):
                self.assertEqual(V.credits(name), (name,))
                self.assertEqual(V.artist_keys(name), frozenset({V.artist_key(name)}))
        self.assertEqual(V.song_key("AC/DC", "Thunderstruck"), "acdc|thunderstruck")

    def test_real_multi_artist_credits_still_split(self):
        self.assertEqual(V.credits("Daft Punk, Pharrell Williams"), ("Daft Punk", "Pharrell Williams"))
        self.assertEqual(V.credits("Iron & Wine; Ben Bridwell"), ("Iron & Wine", "Ben Bridwell"))
        self.assertEqual(V.credits("AC/DC feat. Axl Rose"), ("AC/DC", "Axl Rose"))
        self.assertEqual(V.credits("Kabát ft. Olympic"), ("Kabát", "Olympic"))
        self.assertEqual(V.credits("Simon & Garfunkel featuring Paul"), ("Simon & Garfunkel", "Paul"))
        self.assertEqual(V.credits("Godzilla vs. Kong"), ("Godzilla", "Kong"))

    def test_known_comma_names_survive_in_old_records(self):
        for name in COMMA:
            with self.subTest(name=name):
                self.assertEqual(V.credits(name), (name,))
                self.assertEqual(V.credits(f"{name}, Kabát"), (name, "Kabát"))
                self.assertEqual(V.credits(f"Kabát, {name} feat. Olympic"), ("Kabát", name, "Olympic"))


class Structured(unittest.TestCase):
    def test_youtube_music_list_decides(self):
        for name in SINGLE + COMMA:
            with self.subTest(name=name):
                t = ytm("v1", "Song", name)
                self.assertEqual(V.credits(t.artist), (name,))
        duo = ytm("zprava00001", "Jasná zpráva", "Olympic", "Petr Janda")
        self.assertEqual(duo.artist, "Olympic, Petr Janda")
        self.assertEqual(V.credits(duo.artist), ("Olympic", "Petr Janda"))
        self.assertEqual(V.artist_keys(duo.artist) & {"olympic", "petrjanda"}, {"olympic", "petrjanda"})
        mixed = ytm("v2", "Song", "AC/DC", "Earth, Wind & Fire", "Iron & Wine")
        self.assertEqual(V.credits(mixed.artist), ("AC/DC", "Earth, Wind & Fire", "Iron & Wine"))

    def test_comma_name_learned_from_youtube_music(self):
        name = "Hrabě, Kočka & Myš"  # není v krátkém seznamu známých jmen
        self.assertEqual(V.credits(f"{name}, Kabát"), ("Hrabě", "Kočka & Myš", "Kabát"))
        t = ytm("v3", "Song", name, "Kabát")
        self.assertEqual(t.artist, f"{name}, Kabát")
        # od teď drží pohromadě i v textu bez seznamu (historie, hlasy)
        self.assertEqual(V.credits(t.artist), (name, "Kabát"))
        self.assertEqual(V.credits(name), (name,))
        self.assertIn(V.artist_key(name), V.artist_keys(f"Olympic, {name}"))

    def test_olympic_and_petr_janda_counts_for_both(self):
        b = VoteBook(cfg=Config(**DEFAULTS))
        duo = ytm("zprava00001", "Jasná zpráva", "Olympic", "Petr Janda")
        for v in ("c1", "c2", "c3"):
            b.cast(ARTIST, v, -1, artist="Petr Janda")
        self.assertEqual(b.blocked(duo), ARTIST)
        self.assertEqual(b.blocked(Track("x", "Song", "Petr Janda")), ARTIST)
        self.assertIsNone(b.blocked(Track("y", "Song", "Olympic")))
        self.assertEqual(V.song_key(duo.artist, duo.title), "olympic|jasna zprava")


class KeysUnchanged(unittest.TestCase):
    def test_normal_names_keep_their_keys(self):
        for artist, title in NORMAL:
            with self.subTest(artist=artist):
                self.assertEqual(V.song_key(artist, title), artistfix.old_song_key(artist, title))
                self.assertEqual(V.artist_keys(artist), artistfix._old_artist_keys(artist))
                self.assertEqual(V.credits(artist), artistfix.old_credits(artist))

    def test_bans_favourites_and_no_repeat_for_whole_names(self):
        b = VoteBook(cfg=Config(**DEFAULTS))
        for v in ("c1", "c2", "c3"):
            b.cast(ARTIST, v, -1, artist="AC/DC")
            b.cast(ARTIST, v, 1, artist="Kabát")
        self.assertEqual(b.tally(ARTIST, "acdc").status, V.BANNED)
        self.assertEqual(b.blocked(Track("t1", "Thunderstruck", "AC/DC")), ARTIST)
        self.assertIsNone(b.blocked(Track("t2", "Song", "DC Talk")))
        self.assertIsNone(b.blocked(Track("t3", "Song", "AC")))
        self.assertTrue(b.is_favourite(Track("t4", "Pohoda", "Kabát")))
        # neopakování: jiné nahrání AC/DC je tatáž píseň, "DC Talk" ne
        a = repeats.song_keys("AC/DC", "Thunderstruck (Live at River Plate)")
        self.assertEqual(a, ["acdc|thunderstruck"])
        self.assertFalse(set(a) & set(repeats.song_keys("DC Talk", "Thunderstruck")))
        self.assertEqual(repeats.song_keys("Kabát - Topic", "Malá dáma [Live]"), ["kabat|mala dama"])


class Sheets(unittest.TestCase):
    def test_vote_sheet_offers_acdc_once(self):
        b = VoteBook(cfg=Config(**DEFAULTS))
        d = b.detail(Track("acdc0000001", "Thunderstruck", "AC/DC"), viewer="c1")
        self.assertEqual([a["name"] for a in d["artists"]], ["AC/DC"])
        d = b.detail(ytm("ewf00000001", "September", "Earth, Wind & Fire"), viewer="c1")
        self.assertEqual([a["name"] for a in d["artists"]], ["Earth, Wind & Fire"])
        d = b.detail(ytm("zprava00001", "Jasná zpráva", "Olympic", "Petr Janda"), viewer="c1")
        self.assertEqual([a["name"] for a in d["artists"]], ["Olympic", "Petr Janda"])

    def test_ai_sheet_offers_acdc_once(self):
        book = A.AiBook(seed=None)
        cur = {"id": "acdc0000001", "title": "Thunderstruck", "artist": "AC/DC"}
        ai_api.annotate(SimpleNamespace(ai=book), cur)
        self.assertEqual([(a["name"], a["key"]) for a in cur["ai"]], [("AC/DC", "acdc")])

    def test_web_track_carries_comma_names(self):
        self.assertNotIn("credits", _track_dict(Track("a", "T", "AC/DC")))
        self.assertNotIn("credits", _track_dict(Track("a", "T", "Daft Punk, Pharrell Williams")))
        d = _track_dict(Track("a", "September", "Earth, Wind & Fire, Kabát"))
        self.assertEqual(d["credits"], ["Earth, Wind & Fire", "Kabát"])

    def test_page_splits_like_the_server(self):
        html = (Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html").read_text()
        body = html[html.index("function splitArtists"):html.index("function openSheet")]
        self.assertIn("t.credits", body)
        self.assertNotIn("&", body.split("split(")[1].split(")\n")[0])
        self.assertIn("splitArtists(t).map(", html)


# ---- oprava uložených klíčů (artistfix) ----

OLD_DB = """
INSERT INTO plays (video_id,title,artist,ts,outcome) VALUES
  ('acdc0000001','Thunderstruck','AC/DC',1,'finished'),
  ('iron0000001','Flightless Bird, American Mouth','Iron & Wine',2,'finished'),
  ('xindl000001','Anděla','Xindl X',3,'finished'),
  ('wendy000001','Song','Wendy',4,'finished'),
  ('wendy000002','Touch Theme','Wendy & Lisa',5,'finished'),
  ('roses000001','Song','Wine & Roses',6,'finished'),
  ('kabat000001','Pohoda','Kabát',7,'finished');
INSERT INTO votes VALUES
  ('artist','ac','c-radek',1,'Radek','','AC',NULL,10),
  ('artist','dc','c-radek',1,'Radek','','DC',NULL,11),
  ('artist','acdc','c-jana',1,'Jana','','AC/DC',NULL,30),
  ('artist','ac','c-jana',-1,'Jana','','AC',NULL,12),
  ('artist','xindl','c-petr',-1,'Petr','','Xindl',NULL,13),
  ('artist','wendy','c-petr',1,'Petr','','Wendy',NULL,14),
  ('artist','wine','c-petr',1,'Petr','','Wine',NULL,15),
  ('artist','kabat','c-petr',1,'Petr','','Kabát',NULL,16),
  ('song','ac|thunderstruck','c-radek',1,'Radek','acdc0000001','AC/DC','Thunderstruck',17),
  ('song','iron|flightless bird american mouth','c-petr',1,'Petr','iron0000001','Iron & Wine',
   'Flightless Bird, American Mouth',18),
  ('song','kabat|pohoda','c-petr',1,'Petr','kabat000001','Kabát','Pohoda',19);
INSERT INTO imports (id,client,who,playlist_id,title,created,fetched) VALUES
  ('imp1','c-petr','Petr','PL1','Mix',1,1);
INSERT INTO import_items VALUES
  ('imp1','wendy|touch theme','wendy000002','Wendy & Lisa','Touch Theme',1,0),
  ('imp1','kabat|pohoda','kabat000001','Kabát','Pohoda',1,1);
INSERT INTO ai_flags (key,name,state,source,ts) VALUES
  ('dc','DC','ai','clovek',20),
  ('papizon','Papizon','ai','clovek',21);
"""


def old_store() -> Path:
    d = Path(tempfile.mkdtemp(dir=_TMP))
    Store(d / "state.db").close()  # schéma
    db = sqlite3.connect(d / "state.db")
    db.executescript(OLD_DB)
    db.commit()
    db.close()
    return d / "state.db"


def rows(path: Path, sql: str) -> list:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class Migration(unittest.TestCase):
    def test_fragments_go_back_to_the_whole_name_once(self):
        path = old_store()
        events = Path(os.environ["YTDJ_EVENTS_FILE"])
        before = events.read_text().count('"votes.key_fix"') if events.exists() else 0
        store = Store(path, background=True)
        b = VoteBook(store, Config(**DEFAULTS))
        b.ai = A.AiBook(store, seed=None)
        with self.assertLogs("ytdj.artistfix", "WARNING") as logs:
            run(b.aload())
        self.assertTrue(any("'ac'" in m and "AC/DC" in m for m in logs.output))
        votes = {(t, k, v): (vote, a) for t, k, v, vote, a in
                 rows(path, "SELECT target,key,voter,vote,artist FROM votes")}
        # Radek: 👍 "AC" i "DC" → jeden 👍 AC/DC (novější platí)
        self.assertEqual(votes[(ARTIST, "acdc", "c-radek")], (1, "AC/DC"))
        # Jana: vlastní hlas na AC/DC je novější než starý 👎 "AC" → ten zmizí
        self.assertEqual(votes[(ARTIST, "acdc", "c-jana")], (1, "AC/DC"))
        self.assertEqual(votes[(ARTIST, "xindlx", "c-petr")], (-1, "Xindl X"))
        for frag in ("ac", "dc", "xindl"):
            self.assertFalse([k for k in votes if k[1] == frag], frag)
        # "Wendy" hrála i sama → skutečný interpret, zůstává; "Wine" je ze dvou jmen → nejisté
        self.assertIn((ARTIST, "wendy", "c-petr"), votes)
        self.assertIn((ARTIST, "wine", "c-petr"), votes)
        self.assertIn((ARTIST, "kabat", "c-petr"), votes)
        self.assertIn((SONG, "acdc|thunderstruck", "c-radek"), votes)
        self.assertIn((SONG, "ironwine|flightless bird american mouth", "c-petr"), votes)
        self.assertIn((SONG, "kabat|pohoda", "c-petr"), votes)
        self.assertEqual(sorted(k for _, k in rows(path, "SELECT import_id,key FROM import_items")),
                         ["kabat|pohoda", "wendylisa|touch theme"])
        self.assertEqual(sorted(rows(path, "SELECT key,name FROM ai_flags")),
                         [("acdc", "AC/DC"), ("papizon", "Papizon")])
        # v paměti už jen celé jméno
        self.assertEqual(b.tally(ARTIST, "acdc").up, 2)
        self.assertEqual(b.tally(ARTIST, "ac").up + b.tally(ARTIST, "ac").down, 0)
        self.assertEqual(b.tally(SONG, "acdc|thunderstruck").up, 1)
        self.assertIn("acdc", b.ai.active)
        self.assertEqual(rows(path, "SELECT name FROM fixes"), [(artistfix.FIX_NAME,)])
        evs = [json.loads(x) for x in events.read_text().splitlines() if '"votes.key_fix"' in x]
        self.assertEqual(len(evs), before + 1)
        self.assertEqual(evs[-1]["artists"], {"ac": "acdc", "dc": "acdc", "xindl": "xindlx"})
        store.close()

        # druhý start: nic nečte ani nepřepisuje
        again = Store(path, background=True)
        b2 = VoteBook(again, Config(**DEFAULTS))
        self.assertIsNone(artistfix.run(again))
        run(b2.aload())
        self.assertEqual(b2.tally(ARTIST, "acdc").up, 2)
        evs2 = events.read_text().count('"votes.key_fix"')
        self.assertEqual(evs2, before + 1)
        again.close()

    def test_plan_on_clean_data_changes_nothing(self):
        votes = [(ARTIST, "kabat", "c1", 1, "", "", "Kabát", "", 1.0),
                 (ARTIST, "acdc", "c1", 1, "", "", "AC/DC", "", 2.0),
                 (SONG, "acdc|thunderstruck", "c1", 1, "", "v", "AC/DC", "Thunderstruck", 3.0),
                 (SONG, "olympic|jasna zprava", "c1", 1, "", "v", "Olympic, Petr Janda",
                  "Jasná zpráva", 4.0)]
        p = artistfix.plan(votes, plays=["AC/DC", "Kabát", "Olympic, Petr Janda"])
        self.assertFalse(p)
        self.assertEqual(p.rows, 0)


if __name__ == "__main__":
    unittest.main()
