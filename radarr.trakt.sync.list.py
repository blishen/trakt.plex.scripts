#!/usr/bin/env python
# coding=utf-8
from __future__ import print_function

import json
import os
import re
import sys
import time
from pprint import pprint
from datetime import datetime

import requests

try:
    import config
except ImportError:
    print(('Please see config.py.example, update the '
           'values and rename it to config.py'))
    sys.exit(1)

HERE = os.path.abspath(os.path.dirname(__file__))
CACHEFILE = os.path.join(HERE, '.local.db')
APIURL = 'https://api.trakt.tv'

localdb = {}
if os.path.exists(CACHEFILE):
    with open(CACHEFILE, 'r') as fp:
        localdb = json.load(fp)


def db_set(key, value):
    localdb[key] = value
    with open(CACHEFILE, 'w') as fp:
        json.dump(localdb, fp, indent=2, sort_keys=True)


def get_oauth_headers():
    headers = {
        'Content-type': 'application/json',
        'trakt-api-key': config.CLIENT_ID,
        'trakt-api-version': '2',
    }

    access_token_expires = float(localdb.get('access_token_expires', 0))
    access_token = localdb.get('access_token')
    refresh_token = localdb.get('refresh_token')

    if access_token_expires > time.time() and access_token:
        headers['Authorization'] = 'Bearer %s' % access_token

    else:
        print('Acquiring access_token, refresh_token={0}...'.format(
            refresh_token
        ))
        if refresh_token:
            req = requests.post(
                '{0}/oauth/token'.format(APIURL),
                headers=headers,
                json={
                    'refresh_token': refresh_token,
                    'client_id': config.CLIENT_ID,
                    'client_secret': config.CLIENT_SECRET,
                    'redirect_uri': 'urn:ietf:wg:oauth:2.0:oob',
                    'grant_type': 'refresh_token',
                })
            res = req.json()
            db_set('access_token', res['access_token'])
            db_set('refresh_token', res['refresh_token'])
            db_set('access_token_expires', time.time() + res['expires_in'] - (14 * 24 * 3600))
            print('New access_token acquired!')
        else:
            print('No refresh_token, manual action needed...')
            req = requests.post(
                '{0}/oauth/device/code'.format(APIURL),
                json={
                    'client_id': config.CLIENT_ID,
                }
            )
            assert req.ok, (req, req.content)

            data = req.json()
            start_time = time.time()

            print('Go to {verification_url} and enter code {user_code}'.format(
                verification_url=data['verification_url'],
                user_code=data['user_code'],
            ))

            interval = data['interval']

            while 1:
                if start_time + data['expires_in'] < time.time():
                    print('Too late, you need to start from beginning :(')
                    sys.exit(1)

                time.sleep(interval)
                req = requests.post(
                    '{0}/oauth/device/token'.format(APIURL),
                    json={
                        'client_id': config.CLIENT_ID,
                        'client_secret': config.CLIENT_SECRET,
                        'code': data['device_code'],
                    }
                )

                if req.status_code == 200:
                    res = req.json()
                    print('New tokens acquired!')
                    db_set('access_token', res['access_token'])
                    db_set('refresh_token', res['refresh_token'])
                    db_set('access_token_expires', time.time() + res['expires_in'] - (14 * 24 * 3600))
                    break
                elif req.status_code == 400:
                    print('Pending - waiting for the user to authorize your app')
                elif req.status_code == 404:
                    print('Not Found - invalid device_code')
                    return
                elif req.status_code == 409:
                    print('Already Used - user already approved this code')
                    return
                elif req.status_code == 410:
                    print('Expired - the tokens have expired, restart the process')
                    return
                elif req.status_code == 418:
                    print('Denied - user explicitly denied this code')
                    return
                elif req.status_code == 429:
                    print('Slow Down - your app is polling too quickly')
                    time.sleep(interval)
        headers['Authorization'] = 'Bearer %s' % res['access_token']
    return headers


def get_oauth_request(path, *args, **kwargs):
    headers = get_oauth_headers()
    if 'headers' in kwargs:
        headers.update(kwargs.pop('headers'))
    req = requests.get(
        '{0}/{1}'.format(APIURL, path), headers=headers, **kwargs
    )
    assert req.ok, (req, req.content)
    return req.json()


def post_oauth_request(path, data, *args, **kwargs):
    req = requests.post(
        '{0}/{1}'.format(APIURL, path),
        json=data, headers=get_oauth_headers(), **kwargs
    )
    assert req.ok, (req, req.content)
    return req

def put_oauth_request(path, data, *args, **kwargs):
    req = requests.put(
        '{0}/{1}'.format(APIURL, path),
        json=data, headers=get_oauth_headers(), **kwargs
    )
    assert req.ok, (req, req.content)
    return req

def get_list_id(name):
    key = 'list-id:{0}'.format(name)
    if key in localdb:
        return localdb[key]
    req = get_oauth_request('users/me/lists')
    existing_lists = [x['name'] for x in req]
    if name not in existing_lists:
        post_oauth_request('users/me/lists', data={
            'name': name,
        })
        time.sleep(0.5)
        req = get_oauth_request('users/me/lists')
    res = [x for x in req if x['name'] == name]
    if not res:
        raise Exception('Could not find the list "{0}" :('.format(name))
    list_id = res[0]['ids']['trakt']
    db_set(key, list_id)   
    return list_id

def get_trakt_collection(list_id):
    req = get_oauth_request('users/me/lists/{0}/items'.format(list_id))
    trakt_movies = set()
    for movie in req:
        imdb = movie["movie"]["ids"]["imdb"]
        trakt_movies.add(imdb)
    return trakt_movies

def get_radarr_collection():
    radarr_movies = set()
    radarr_movies_all = set()
    radarr_url = config.RADARR_URL
    radarr_key = config.RADARR_SESSION
    radarrSession = requests.Session()
    radarrSession.trust_env = False
    radarrMovies = radarrSession.get('{0}/api/movie?apikey={1}'.format(radarr_url, radarr_key))
    if radarrMovies.status_code != 200:
      print('Radarr server error - response {}'.format(radarrMovies.status_code))
      sys.exit(0)
    for movie in radarrMovies.json():
        radarr_movies_all.add(movie['imdbId'])
        if movie['downloaded']:
            radarr_movies.add(movie['imdbId'])
    return radarr_movies, radarr_movies_all

def main():
    list_id = get_list_id('My Collection')
    # update list description
    put_oauth_request('users/me/lists/{0}'.format(list_id), data={
        'description': 'Updated at ' + datetime.today().strftime('%Y-%m-%d')
    })
    time.sleep(0.5) 

    list_id_rw = get_list_id('Radarr Watchlist')
    # update list description
    put_oauth_request('users/me/lists/{0}'.format(list_id_rw), data={
        'description': 'Updated at ' + datetime.today().strftime('%Y-%m-%d')
    })
    time.sleep(0.5) 

    radarr_collection, radarr_collection_rw = get_radarr_collection()
    trakt_collection = get_trakt_collection(list_id)
    trakt_collection_rw = get_trakt_collection(list_id_rw)
    
    post_data_add = []
    post_data_remove = []

    for imdb in radarr_collection:
        if imdb in trakt_collection:
            continue
        print('Movie in Radarr but not in Trakt. Will add {0}...'.format(imdb))
        post_data_add.append({'ids': {'imdb': '{0}'.format(imdb)}})    
    pprint(post_oauth_request('users/me/lists/{0}/items'.format(list_id), data={'movies': post_data_add}).json())


    for imdb in trakt_collection:
        if imdb in radarr_collection:
            continue
        print('Movie in Trakt but not in Radarr. Will delete {0}...'.format(imdb))
        post_data_remove.append({'ids': {'imdb': '{0}'.format(imdb)}})
    pprint(post_oauth_request('users/me/lists/{0}/items/remove'.format(list_id), data={'movies': post_data_remove}).json())

    post_data_add = []
    post_data_remove = []

    for imdb in radarr_collection_rw:
        if imdb in trakt_collection_rw:
            continue
        print('Movie in Radarr but not in Trakt. Will add {0}...'.format(imdb))
        post_data_add.append({'ids': {'imdb': '{0}'.format(imdb)}})    
    pprint(post_oauth_request('users/me/lists/{0}/items'.format(list_id_rw), data={'movies': post_data_add}).json())


    for imdb in trakt_collection_rw:
        if imdb in radarr_collection_rw:
            continue
        print('Movie in Trakt but not in Radarr. Will delete {0}...'.format(imdb))
        post_data_remove.append({'ids': {'imdb': '{0}'.format(imdb)}})
    pprint(post_oauth_request('users/me/lists/{0}/items/remove'.format(list_id_rw), data={'movies': post_data_remove}).json())

if __name__ == '__main__':
    main()
