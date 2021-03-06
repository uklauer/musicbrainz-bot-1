#!/usr/bin/python

import sys
import re
import sqlalchemy
import solr
from simplemediawiki import MediaWiki
from editing import MusicBrainzClient
import pprint
import urllib
import time
from mbbot.wp.wikipage import WikiPage
from utils import mangle_name, join_names, out, colored_out, bcolors, escape_query, quote_page_title, wp_is_canonical_page
import config as cfg

engine = sqlalchemy.create_engine(cfg.MB_DB)
db = engine.connect()
db.execute("SET search_path TO musicbrainz, %s" % cfg.BOT_SCHEMA_DB)

wp_lang = sys.argv[1] if len(sys.argv) > 1 else 'en'

wp = MediaWiki('http://%s.wikipedia.org/w/api.php' % wp_lang)

suffix = '_' + wp_lang if wp_lang != 'en' else ''
wps = solr.SolrConnection('http://localhost:8983/solr/wikipedia' + suffix)

mb = MusicBrainzClient(cfg.MB_USERNAME, cfg.MB_PASSWORD, cfg.MB_SITE)

"""
CREATE TABLE bot_wp_rg_link (
    gid uuid NOT NULL,
    lang character varying(2),
    processed timestamp with time zone DEFAULT now(),
    CONSTRAINT bot_wp_rg_link_pkey PRIMARY KEY (gid, lang)
);
"""


acceptable_countries_for_lang = {
    'fr': ['FR', 'MC']
}
acceptable_countries_for_lang['en'] = acceptable_countries_for_lang['fr']

query_params = []
no_country_filter = (wp_lang == 'en') and ('en' not in acceptable_countries_for_lang or len(acceptable_countries_for_lang['en']) == 0)
if no_country_filter:
    # Hack to avoid having an SQL error with an empty IN clause ()
    in_country_clause = 'FALSE'
else:
    placeHolders = ','.join(['%s'] * len(acceptable_countries_for_lang[wp_lang]))
    in_country_clause = "%s IN (%s)" % ('iso.code', placeHolders)
    query_params.extend(acceptable_countries_for_lang[wp_lang])
query_params.append(wp_lang)

query = """
WITH
    rgs_wo_wikipedia AS (
        SELECT rg.id
        FROM release_group rg
        LEFT JOIN (SELECT l.entity0 AS id
            FROM l_release_group_url l
            JOIN url u ON l.entity1 = u.id AND u.url LIKE 'http%%://""" + wp_lang + """.wikipedia.org/wiki/%%'
            WHERE l.link IN (SELECT id FROM link WHERE link_type = 89)
        ) wpl ON wpl.id = rg.id
        LEFT JOIN (SELECT l.entity0 AS id
            FROM l_release_group_url l
            JOIN url u ON l.entity1 = u.id
            WHERE l.link IN (SELECT id FROM link WHERE link_type = 353)
        ) wdl ON wdl.id = rg.id
        LEFT JOIN (SELECT acn.artist_credit
            FROM artist_credit_name acn
            JOIN artist a ON acn.artist = a.id
            LEFT JOIN area ON a.area = area.id
            LEFT JOIN iso_3166_1 iso ON iso.area = area.id
            WHERE """ + in_country_clause + """
            GROUP BY acn.artist_credit HAVING count(iso.code) = 1
        ) tc ON rg.artist_credit = tc.artist_credit
        WHERE rg.artist_credit > 2 AND wpl.id IS NULL AND wdl.ID IS NULL
            AND (rg.type IS NULL OR rg.type IN (SELECT id FROM release_group_primary_type WHERE name IN ('Album')))
            AND (tc.artist_credit IS NOT NULL """ + (' OR TRUE' if no_country_filter else '') + """)
            AND rg.edits_pending = 0
        ORDER BY rg.artist_credit, rg.id
        LIMIT 100000
    )
SELECT rg.id, rg.gid, rg.name, ac.name, string_agg(rgtn.name, ',') AS rg_secondary_types, b.processed
FROM rgs_wo_wikipedia ta
JOIN release_group rg ON ta.id=rg.id
JOIN artist_credit ac ON rg.artist_credit=ac.id
LEFT JOIN bot_wp_rg_link b ON rg.gid = b.gid AND b.lang = %s
LEFT JOIN release_group_secondary_type_join rgst ON rg.id = rgst.release_group
LEFT JOIN release_group_secondary_type rgtn ON rgst.secondary_type = rgtn.id
GROUP BY rg.artist_credit, rg.id, rg.gid, rg.name, ac.name, b.processed
ORDER BY b.processed NULLS FIRST, rg.artist_credit, rg.id
LIMIT 1000
"""

query_album_tracks = """
SELECT DISTINCT t.name
FROM track t
JOIN medium m ON t.medium=m.id
JOIN release r ON m.release=r.id
WHERE r.release_group = %s
"""

category_re = {}
category_re['en'] = re.compile(r'\[\[Category:(.+?)(?:\|.*?)?\]\]')
category_re['fr'] = re.compile(r'\[\[Cat\xe9gorie:(.+?)\]\]')

for rg_id, rg_gid, rg_name, ac_name, rg_sec_types, processed in db.execute(query, query_params):
    colored_out(bcolors.OKBLUE, 'Looking up release group "%s" http://musicbrainz.org/release-group/%s' % (rg_name, rg_gid))
    matches = wps.query(escape_query(rg_name), defType='dismax', qf='name', rows=100).results
    last_wp_request = time.time()
    for match in matches:
        title = match['name']
        if mangle_name(re.sub(' \(.+\)$', '', title)) != mangle_name(rg_name) and mangle_name(title) != mangle_name(rg_name):
            continue
        delay = time.time() - last_wp_request
        if delay < 1.0:
            time.sleep(1.0 - delay)
        last_wp_request = time.time()
        wikipage = WikiPage.fetch('http://%s.wikipedia.org/wiki/%s' % (wp_lang, title))
        page_orig = wikipage.text
        if not page_orig:
            continue
        page_title = title
        colored_out(bcolors.HEADER, ' * trying article %s' % (title,))
        page = mangle_name(page_orig)

        is_canonical, reason = wp_is_canonical_page(title, page_orig)
        if (not is_canonical):
            out(' * %s, skipping' % reason)
            continue

        categories = category_re[wp_lang].findall(page_orig)
        is_album_page = False
        for category in categories:
            if wp_lang == 'en':
                if category.lower().endswith(' albums'):
                    is_album_page = True
                    break
                if category.lower().endswith(' soundtracks'):
                    is_album_page = True
                    break
                #if category.lower().endswith(' singles'):
                #    is_album_page = True
                #    break
            if wp_lang == 'fr':
                if category.startswith('Album '):
                    is_album_page = True
                    break
        if not is_album_page:
            out('  => not an album page, skipping')
            continue
        if mangle_name(ac_name) not in page:
            out('  => artist name not found')
            continue
        found_tracks = []
        tracks = set([r[0] for r in db.execute(query_album_tracks, (rg_id,))])
        tracks_to_ignore = set()
        for track in tracks:
            mangled_track = mangle_name(track)
            if len(mangled_track) <= 4 or mangle_name(rg_name) in mangle_name(track):
                tracks_to_ignore.add(track)
        tracks -= tracks_to_ignore
        if len(tracks) < 5:
            continue
        for track in tracks:
            mangled_track = mangle_name(track)
            if len(mangled_track) > 4 and mangled_track in page:
                found_tracks.append(track)
        ratio = len(found_tracks) * 1.0 / len(tracks)
        out(' * ratio: %s, has tracks: %s, found tracks: %s' % (ratio, len(tracks), len(found_tracks)))
        min_ratio = 0.7 if len(rg_name) > 4 else 1.0
        if ratio < min_ratio:
            colored_out(bcolors.WARNING, '  => ratio too low (min = %s)' % min_ratio)
            continue
        auto = ratio > 0.75 and (rg_sec_types is None or ('Compilation' not in rg_sec_types and 'Soundtrack' not in rg_sec_types))

        wp_url = 'http://%s.wikipedia.org/wiki/%s' % (wp_lang, quote_page_title(page_title),)
        wd_url = 'http://www.wikidata.org/wiki/%s' % wikipage.wikidata_id.upper()
        text = 'Wikidata identifier found from matching Wikipedia page %s. The page mentions artist "%s" and %s.' % (wp_url, ac_name, join_names('track', found_tracks),)
        colored_out(bcolors.OKGREEN, ' * linking to %s' % (wd_url,))
        out(' * edit note: %s' % (text,))
        time.sleep(5)
        mb.add_url("release_group", rg_gid, 353, wd_url, text, auto=auto)
        break
    if processed is None:
        db.execute("INSERT INTO bot_wp_rg_link (gid, lang) VALUES (%s, %s)", (rg_gid, wp_lang))
    else:
        db.execute("UPDATE bot_wp_rg_link SET processed = now() WHERE (gid, lang) = (%s, %s)", (rg_gid, wp_lang))
