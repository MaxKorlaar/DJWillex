import os
import sys
import time
import shlex
import shutil
import inspect
import aiohttp
import discord
import asyncio
import traceback
import random
import urllib.parse
import urllib.request
import json
import hashlib
import datetime
import cachetclient.cachet as cachet
import configparser

from discord import utils
from discord.object import Object
from discord.enums import ChannelType
from discord.voice_client import VoiceClient
from discord.ext.commands.bot import _get_variable

from io import BytesIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from random import choice, shuffle
from collections import defaultdict

from musicbot.playlist import Playlist
from musicbot.player import MusicPlayer
from musicbot.config import Config, ConfigDefaults
from musicbot.permissions import Permissions, PermissionsDefaults
from musicbot.utils import load_file, write_file, sane_round_int

from . import exceptions
from . import downloader
from .opus_loader import load_opus_lib
from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH



load_opus_lib()
startTime = time.time()
has_restarted = 1

cachet_config = configparser.ConfigParser()
cachet_config.read("cachet.ini")

class SkipState:
    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count

class Response:
    def __init__(self, content, reply=False, delete_after=0):
        self.content = content
        self.reply = reply
        self.delete_after = delete_after


class MusicBot(discord.Client):
    def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file):
        self.players = {}
        self.the_voice_clients = {}
        self.locks = defaultdict(asyncio.Lock)
        self.voice_client_connect_lock = asyncio.Lock()
        self.voice_client_move_lock = asyncio.Lock()

        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        self.exit_signal = None
        self.init_ok = False
        self.cached_client_id = None

        if not self.autoplaylist:
            print("Waarschuwing: De autoplaylist is op dit moment leeg. Bot wordt uitgeschakeld.")
            self.config.auto_playlist = False

        # TODO: Do these properly
        ssd_defaults = {'last_np_msg': None, 'auto_paused': False}
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("Alleen de eigenaar mag dit commando gebruiken", expire_in=30)

        return wrapper

    @staticmethod
    def _fixg(x, dp=2):
        return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

    def _get_owner(self, voice=False):
        if voice:
            for server in self.servers:
                for channel in server.channels:
                    for m in channel.voice_members:
                        if m.id == self.config.owner_id:
                            return m
        else:
            return discord.utils.find(lambda m: m.id == self.config.owner_id, self.get_all_members())

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    # TODO: autosummon option to a specific channel
    async def _auto_summon(self):
        owner = self._get_owner(voice=True)
        if owner:
            self.safe_print("Eigenaar gevonden in \"%s\", trachten om het kanaal te betreden..." % owner.voice_channel.name)
            # TODO: Effort
            await self.cmd_summon(owner.voice_channel, owner, None)
            return owner.voice_channel



    async def uptime(self):
        timeSeconds = (time.time() - startTime)
        return timeSeconds

    async def _autorestart(self):
        now = datetime.datetime.now()
        global has_restarted
        if now.hour == 3 and has_restarted == 0:
            has_restarted = 1
            raise exceptions.RestartSignal

        else:
            await asyncio.sleep(3600)
            has_restarted = 0
            await self._autorestart()
            return ''

    async def _autojoin_channels(self, channels):
        joined_servers = []

        for channel in channels:
            if channel.server in joined_servers:
                print("Ik zit al in kanaal %s" % channel.server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                self.safe_print("Probeert automatisch te joinen bij %s in %s" % (channel.name, channel.server.name))

                chperms = channel.permissions_for(channel.server.me)

                if not chperms.connect:
                    self.safe_print("Kan niet in \"%s\" kanaal komen; geen permissies." % channel.name)
                    continue

                elif not chperms.speak:
                    self.safe_print("Kan kanaal \"%s\" niet binnengaan; geen toestemming om te praten." % channel.name)
                    continue

                try:
                    player = await self.get_player(channel, create=True)

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        await self.on_player_finished_playing(player)

                    joined_servers.append(channel.server)
                except Exception as e:
                    if self.config.debug_mode:
                        traceback.print_exc()
                    print("Binnengaan mislukt", channel.name)

            elif channel:
                print("Ik ga %s op %s binnen, dat is een tekst kanaal." % (channel.name, channel.server.name))

            else:
                print("Ongeldig kanaal: " + channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "Je kan dit commando niet gebruiken als je niet in een spraakkanaal zit (%s)" % vc.name, expire_in=30)
            return False

    async def generate_invite_link(self, *, permissions=None, server=None):
        if not self.cached_client_id:
            appinfo = await self.application_info()
            self.cached_client_id = appinfo.id

        return discord.utils.oauth_url(self.cached_client_id, permissions=permissions, server=server)

    async def get_voice_client(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Het opgegeven kanaal moet een spraakkanaal zijn.')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.the_voice_clients:
                return self.the_voice_clients[server.id]

            s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
            _voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

            await self.ws.voice_state(server.id, channel.id)

            s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
            voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
            session_id = s_id_data.get('session_id')

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }
            voice_client = VoiceClient(**kwargs)
            self.the_voice_clients[server.id] = voice_client

            retries = 3
            for x in range(retries):
                try:
                    print("Wacht op verbinding...")
                    await asyncio.wait_for(voice_client.connect(), timeout=10, loop=self.loop)
                    print("Verbonden.")
                    break
                except:
                    traceback.print_exc()
                    print("Verbinding mislukt, ik probeer het opnieuw (%s/%s)..." % (x+1, retries))
                    await asyncio.sleep(1)
                    await self.ws.voice_state(server.id, None, self_mute=True)
                    await asyncio.sleep(1)

                    if x == retries-1:
                        raise exceptions.HelpfulError(
                            "Kan geen verbinding maken met spraakkanaal.  "
                            "Iets blokkeert mogelijk UDP verbindingen.",

                            "Dit probleem kan veroorzaakt worden door een firewall die UDP blokkeert.  "
                            "Zoek uit wat UDP blokkeert en schakel het uit.  "
                            "Dit is waarschijnlijk een systeem of anti-virus firewall.  "
                        )

            return voice_client

    async def get_voice_client_storing(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Het aangegeven kanaal moet een spraakkanaal zijn.')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.the_voice_clients:
                return self.the_voice_clients[server.id]

            s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
            _voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

            await self.ws.voice_state(server.id, channel.id)

            s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
            voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
            session_id = s_id_data.get('session_id')

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }
            voice_client = VoiceClient(**kwargs)
            self.the_voice_clients[server.id] = voice_client
            return voice_client

    async def mute_voice_client(self, channel, mute):
        await self._update_voice_state(channel, mute=mute)

    async def deafen_voice_client(self, channel, deaf):
        await self._update_voice_state(channel, deaf=deaf)

    async def move_voice_client(self, channel):
        await self._update_voice_state(channel)

    async def reconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        vc = self.the_voice_clients.pop(server.id)
        _paused = False

        player = None
        if server.id in self.players:
            player = self.players[server.id]
            if player.is_playing:
                player.pause()
                _paused = True

        try:
            await vc.disconnect()
        except:
            print("Fout: de verbinding is verbroken tijdens het opnieuw proberen te verbinden.")
            traceback.print_exc()

        await asyncio.sleep(0.1)

        if player:
            new_vc = await self.get_voice_client(vc.channel)
            player.reload_voice(new_vc)

            if player.is_paused and _paused:
                player.resume()

    async def disconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await self.the_voice_clients.pop(server.id).disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in self.the_voice_clients.copy().values():
            await self.disconnect_voice_client(vc.channel.server)

    async def _update_voice_state(self, channel, *, mute=False, deaf=False):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Het aangegeven kanaal moet een spraakkanaal zijn.')

        # I'm not sure if this lock is actually needed
        with await self.voice_client_move_lock:
            server = channel.server

            payload = {
                'op': 4,
                'd': {
                    'guild_id': server.id,
                    'channel_id': channel.id,
                    'self_mute': mute,
                    'self_deaf': deaf
                }
            }

            await self.ws.send(utils.to_json(payload))
            self.the_voice_clients[server.id].channel = channel

    async def get_player(self, channel, create=False) -> MusicPlayer:
        server = channel.server

        if server.id not in self.players:
            if not create:
                raise exceptions.CommandError(
                    'De bot is niet in een spraakkanaal.  '
                    'Gebruik %ssummon om de bot op te roepen.' % self.config.command_prefix)

            voice_client = await self.get_voice_client(channel)

            playlist = Playlist(self)
            player = MusicPlayer(self, voice_client, playlist) \
                .on('play', self.on_player_play) \
                .on('resume', self.on_player_resume) \
                .on('pause', self.on_player_pause) \
                .on('stop', self.on_player_stop) \
                .on('finished-playing', self.on_player_finished_playing) \
                .on('entry-added', self.on_player_entry_added)

            player.skip_state = SkipState()
            self.players[server.id] = player

        return self.players[server.id]

    async def on_player_play(self, player, entry):
        await self.update_now_playing(entry)
        player.skip_state.reset()

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = '%s - je nummer **%s** speelt nu in %s!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Speelt nu in %s: **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

    async def on_player_resume(self, entry, **_):
        await self.update_now_playing(entry)

    async def on_player_pause(self, entry, **_):
        await self.update_now_playing(entry, True)

    async def on_player_stop(self, **_):
        await self.update_now_playing()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            while self.autoplaylist:
                song_url = choice(self.autoplaylist)
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)

                if not info:
                    self.autoplaylist.remove(song_url)
                    self.safe_print("[Info] Onspeelbaar nummer uit autoplaylist verwijderd: %s" % song_url)
                    write_file(self.config.auto_playlist_file, self.autoplaylist)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    pass  # Wooo playlist
                    # Blarg how do I want to do this

                # TODO: better checks here
                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    print("Fout bij toevoegen van nummer van autoplaylist:", e)
                    continue

                break

            if not self.autoplaylist:
                print("[Waarschuwing] Geen afspeelbare nummers in autoplaylist, bot schakelt zich uit.")
                self.config.auto_playlist = False

    async def on_player_entry_added(self, playlist, entry, **_):
        pass

    async def update_now_playing(self, entry=None, is_paused=False):
        game = None

        if self.user.bot:
            activeplayers = sum(1 for p in self.players.values() if p.is_playing)
            if activeplayers > 1:
                game = discord.Game(name="muziek op %s servers" % activeplayers,type=0)
                entry = None

            elif activeplayers == 1:
                player = discord.utils.get(self.players.values(), is_playing=True)
                entry = player.current_entry

        if entry:
            prefix = u'\u275A\u275A ' if is_paused else ''

            name = u'{}{}'.format(prefix, entry.title)[:128]
            game = discord.Game(name=name,type=0)

        await self.change_presence(game=game)


    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Waarschuwing: Kan geen bericht sturen naar %s, geen permissie." % dest.name)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Waarschuwing: Kan geen bericht sturen naar %s, ongeldig kanaal?" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Waarschuwing: Kan bericht niet verwijderen \"%s\", geen toestemming" % message.clean_content)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Waarschuwing: Kan bericht niet verwijderen \"%s\", bericht niet gevonden" % message.clean_content)

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Waarschuwing: Kan bericht niet aanpassen \"%s\", bericht niet gevonden" % message.clean_content)
            if send_if_fail:
                if not quiet:
                    print("Stuurt alternatief")
                return await self.safe_send_message(message.channel, new)

    def safe_print(self, content, *, end='\n', flush=True):
        sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
        if flush: sys.stdout.flush()

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            if self.config.debug_mode:
                print("Kan niet schrijven naar %s, geen toestemming" % destination)

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: # Can be ignored
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: # Can be ignored
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Email or Password or Token in the options file.  "
                "Remember that each field should be on their own line.")

        finally:
            try:
                self._cleanup()
            except Exception as e:
                print("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            print("Exception in", event)
            print(ex.message)

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            traceback.print_exc()

    async def on_resumed(self):
        for vc in self.the_voice_clients.values():
            vc.main_ws = self.ws

    async def storing(self):
        channel = cachet_config['CACHET']['CHANNEL']
        answer = await self.get_voice_client_storing(self.get_channel(channel))
        answer2 = answer.is_connected()
        if str(answer2) == 'True':
            ENDPOINT  = cachet_config['CACHET']['ENDPOINT']
            API_TOKEN = cachet_config['CACHET']['API_TOKEN']
            components = cachet.Components(endpoint=ENDPOINT, api_token = API_TOKEN)
            ID = cachet_config['CACHET']['ID']
            components.put(id=int(ID), status = 1)
            print('Geen storing')
            await asyncio.sleep(20)
            await self.storing()
        else:
            ENDPOINT = cachet_config['CACHET']['ENDPOINT']
            API_TOKEN = cachet_config['CACHET']['API_TOKEN']
            components = cachet.Components(endpoint=ENDPOINT, api_token=API_TOKEN)
            components.put(id=int(ID), status=3)
            print('Mogelijke storing')
            await asyncio.sleep(20)
            await self.storing()

    async def on_ready(self):
        print('\rConnected!  Musicbot v%s\n' % BOTVERSION)

        if self.config.owner_id == self.user.id:
            raise exceptions.HelpfulError(
                "Your OwnerID is incorrect or you've used the wrong credentials.",

                "The bot needs its own account to function.  "
                "The OwnerID is the id of the owner, not the bot.  "
                "Figure out which one is which and use the correct information.")

        self.init_ok = True

        self.safe_print("Bot:   %s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            self.safe_print("Owner: %s/%s#%s\n" % (owner.id, owner.name, owner.discriminator))

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        elif self.servers:
            print("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        else:
            print("Owner unknown, bot is not on any servers.")
            if self.user.bot:
                print("\nTo make the bot join a server, paste this link in your browser.")
                print("Note: You should be logged into your main account and have \n"
                      "manage server permissions on the server you want the bot to join.\n")
                print("    " + await self.generate_invite_link())

        print()

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            print("Bound to text channels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nNot binding to voice channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print()

        else:
            print("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            print("Autojoining voice chanels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nCannot join text channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            print("Not autojoining any voice channels")
            autojoin_channels = set()

        print()
        print("Options:")

        self.safe_print("  Command prefix: " + self.config.command_prefix)
        print("  Default volume: %s%%" % int(self.config.default_volume * 100))
        print("  Skip threshold: %s votes or %s%%" % (
            self.config.skips_required, self._fixg(self.config.skip_ratio_required * 100)))
        print("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        print("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
        print("  Auto-Playlist: " + ['Disabled', 'Enabled'][self.config.auto_playlist])
        print("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
        print("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            print("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        print("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        print("  Downloaded songs will be %s" % ['deleted', 'saved'][self.config.save_videos])
        print()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                print("Deleting old audio cache")
            else:
                print("Could not delete old audio cache, moving on.")

        if self.config.autojoin_channels:
            await self._autojoin_channels(autojoin_channels)

        elif self.config.auto_summon:
            print("Attempting to autosummon...", flush=True)

            # waitfor + get value
            owner_vc = await self._auto_summon()


            if owner_vc:
                print("Done!", flush=True)  # TODO: Change this to "Joined server/channel"
                if self.config.auto_playlist:
                    print("Starting auto-playlist")
                    await self.on_player_finished_playing(await self.get_player(owner_vc))
            else:
                print("Owner not found in a voice channel, could not autosummon.")
        CACHET_ON = cachet_config['CACHET']['CACHET_ON']
        if CACHET_ON == 'True':
            await self.storing()
        else:
            pass
        # t-t-th-th-that's all folks!

#    def write_lastfm_users(self, users):
#        with open('lastfm.py', 'w') as file:
#        	json.dump(users, file)
#
#    async def cmd_lastfm(self, channel, author, username = ''):
#        if not username:
#            await self.safe_send_message(channel, "Je username is niet ingevuld, gebruik `lastfm username`.")
#        else:
#            users[author.id] = username
#            self.write_lastfm_users(users)
#
#    async def get_lastfm_users(self):
#            if os.path.isfile('lastfm.py'):
#                with open('lastfm.py') as file:
#                    try:
#                        return json.load(file)
#                    except:
#                        with open('lastfm.py', 'w') as file:
#                            selfjson.dump({}, file)

#    async def cmd_scrobble(self, channel, player):
#        split_string = player.current_entry.title.split('-')
#        artist = split_string[0]
#        track = split_string[1]
#        LASTFM_SK = self.get_session_lastfm()
#        LASTFM_APISIG = hashlib.md5('LASTFM_APISIG')
#        url = 'http://ws.audioscrobbler.com/2.0/?method=track.scrobble&artist={0}&track={1}&timestamp={1}&api_key={2}&api_sig={3}&sk={4}'.format(artist, track, int(time.time()), LASTFM_APIKEY, LASTFM_APISIG, LASTFM_SK)
#        json = await post_json_data(url)

    async def cmd_spotify(self, channel, player):
        name = urllib.parse.quote(player.current_entry.title)
        url = 'https://api.spotify.com/v1/search?q={0}&type=track&limit=1'.format(name)
        json = await self.get_json_data(url)
        try:
            tracks = json.get("tracks", None)
            items = tracks.get("items", None)
            spotify_url = items[0]['external_urls']['spotify']
            return Response(spotify_url, delete_after=20)
        except Exception as e:
            return Response('Helaas hebben we het nummer niet kunnen vinden op Spotify', delete_after=20)

#    async def get_token_lasfm(self):
#        LASTFM_APISIGTOKEN = hashlib.md5('LASTFM_APISIG')
#        url = 'http://ws.audioscrobbler.com/2.0/?method=auth.gettoken&api_key={0}&api_sig={1}&format=json'.format=(LASTFM_APIKEY,LASTFM_APISIGTOKEN)
#        async with aiohttp.get(url) as data_token:
#            token = await data_token.json()
#            return token
#    async def get_session_lastfm(self):
#        url = 'http://ws.audioscrobbler.com/2.0/?method=auth.getsession&api_key={0}&token={1}&api_sig={2}&format=json'.format=(LASTFM_APIKEY, self.get_token_lastfm(),LASTFM_APISIGSESSION)
#        LASTFM_APISIGSESSION = hashlib.md5('LASTFM_APISIG')
#        async with aiohttp.get(url) as data_sessionkey:
#            session_key = await data_sessionkey.json()
#            return session_key

    async def get_html_data(self, url):
        async with aiohttp.get(url) as temp:
        	# data = await temp.json()
        	return temp

    async def get_json_data(self, url):
        async with aiohttp.get(url) as temp:
        	data = await temp.json()
        	return data

    async def post_json_data(self, url):
        async with aiohttp.post(url) as temp:
            data = await temp.json()
            return data

    async def cmd_uptime(self, channel):
        timeSeconds = (time.time() - startTime)
        m, s = divmod(timeSeconds, 60)
        h, m = divmod(m, 60)
        string = "%d:%02d:%02d" % (h, m, s)
        return Response(string, delete_after=10)

    async def cmd_help(self, command=None):
        """
        Uitleg:
            ;help [commando]

        Als je een commando opgeeft, krijg je de uitleg voor dat commando te zien.
        Zonder commando wordt een lijst van alle commando's weergegeven.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd:
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__),
                        command_prefix=self.config.command_prefix
                    ),
                    delete_after=60
                )
            else:
                return Response("Commando bestaat niet", delete_after=10)

        else:
            helpmsg = "**Commando's**\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help':
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(self.config.command_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "```"
            helpmsg += "https://github.com/GeoffreyWesthoff/DJWillex"

            return Response(helpmsg, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        Usage:
            ;blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise exceptions.CommandError("Geen mensen in lijst.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                'Ongeldige optie "%s" ingevoerd, gebruik +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] De eigenaar kan niet op de blacklist worden gezet.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s gebruikers zijn op de blacklist gezet' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('geen van deze gebruikers staan op de blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s gebruikers zijn uit de blacklist gehaald' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            ;id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response('je id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        Usage:
            ;joinserver invite_link

        Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
        """

        if self.user.bot:
            url = await self.generate_invite_link()
            return Response(
                "Bot accounts kunnen geen invite links gebruiken, druk hier om me te inviten: \n{}".format(url),
                reply=True, delete_after=30
            )

        try:
            if server_link:
                await self.accept_invite(server_link)
                return Response(":+1:")

        except:
            raise exceptions.CommandError('Invalid URL provided:\n{}\n'.format(server_link), expire_in=30)
    async def cmd_playspotify(self, channel, song_url):
        open_url = song_url.replace('open', 'play')
        spotify_id = open_url.replace('https://play.spotify.com/track/', '')
        url = 'https://api.spotify.com/v1/tracks/{0}'.format(spotify_id)
        json = await self.get_json_data(url)
        try:
            album = json.get('album', None)
            artists = album.get('artists', None)
            artist_name = artists[0]['name']
            print(artist_name)
            track_name = json.get('name', None)
            print(track_name)
            play_command = ';play {0} {1}'.format(artist_name, track_name)
            return Response(play_command, delete_after=1)
        except Exception as e:
            return Response('Er is een fout opgetreden bij het afspelen van de Spotify link', delete_after=20)

    # alias for 'playspotify'
    cmd_ps = cmd_playspotify

    async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
        """
        Uitleg:
            ;play link
            ;play zoektekst

        Voegt een nummer toe aan de wachtrij.  Als je geen link invoert wordt het eerste resultaat van YouTube gepakt.
        """

        song_url = song_url.strip('<>')
        sites = ['dumpert', 'redtube', 'telegraaf']
        for site in sites:
            if site in song_url:
                raise exceptions.CommandError("De site die je wilt gebruiken is niet toegestaan, stuur een PM naar Auxim als deze site toegevoegd zou moeten worden", expire_in=30)
            else:
                pass


        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "Je hebt de limiet voor het aantal nummers in de wachtrij bereikt (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        try:
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError("Die video kan niet worden afgespeeld.", expire_in=30)

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # print("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    "Fout bij informatie uit zoekterm halen, YouTube gaf geen data.  "
                    "Als dit blijft gebeuren moet de bot opnieuw opgestart worden.", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                return

            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an arbitrary number of search results
            if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                raise exceptions.PermissionsError("Je hebt geen toestemming om afspeellijsten aan te vragen", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                raise exceptions.PermissionsError(
                    "Teveel nummers in de afspeellijst (%s > %s)" % (num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
            if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                raise exceptions.PermissionsError(
                    "De nummers in de afspeellijst en de nummers in de wachtrij zijn over de limiet (%s + %s > %s)" % (
                        num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                try:
                    return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                except exceptions.CommandError:
                    raise
                except Exception as e:
                    traceback.print_exc()
                    raise exceptions.CommandError("Fout bij toevoegen aan afspeellijst:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
            # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, so this will have to do.
            # It would probably be a thread to check a few playlists and get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Verkrijgt informatie over afspeellijst van {} nummers {}'.format(
                    num_songs,
                    ', Wachttijd: {} seconden'.format(self._fixg(
                        num_songs * wait_per_song)) if num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.  We need either a loop
            # that sends these every 10 seconds or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
            #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

            entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this would ever break
                        # Unless the first entry starts being played, which would make this a race condition
                if drop_count:
                    print("Verwijderde %s nummers" % drop_count)

            print("Verwerkte {} nummers in {} seconden bij {:.2f}s/nummer, {:+.2g}/onverwachte nummers ({}s)".format(
                listlen,
                self._fixg(ttime),
                ttime / listlen,
                ttime / listlen - wait_per_song,
                self._fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise exceptions.CommandError(
                    "Geen nummers toegevoegd, alle nummers waren te lang (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "**%s** nummers worden nog afgespeeld. Positie in wachtrij: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Nummer duurt langer dan toegestane limiet (%s > %s)" % (info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

            except exceptions.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    print("[Warning] bot fkn dies")

                if self.config.debug_mode:
                    print("[Info] Ging er van uit dat \"%s\" slechts één nummer was, maar was stiekem een playlist" % song_url)
                    print("[Info] Gebruikt \"%s\" in plaats van" % e.use_url)

                return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)


            if random.randrange(1,20) == 10:
                reply_text = ("Rustaagh, ben al bezig **%s** in de lijst te zetten. Positie in wachtrij: %s")
                waitlist_text = ("Rustaagh, je nummer wordt zo in de lijst gezet. Positie in wachtrij: " + str(position))
                # lol --Tony
            elif random.randrange(1,20) == 20:
                reply_text = ("Soms als ik me alleen voel weet ik dat TonyQuark er altijd voor me is... Hoe dan ook, **%s** staat nu in de lijst. Positie in wachtrij: %s")
                waitlist_text = ("Soms als ik me alleen voel weet ik dat TonyQuark er altijd voor me is... Hoe dan ook, je nummer staat in de lijst. Positie in wachtrij: " + str(position))
            else:
                reply_text = ("**%s** wordt straks afgespeeld. Positie in wachtrij: %s")
                waitlist_text = ("Je nummer staat in de lijst. Positie in wachtrij: " + str(position))
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Speelt hierna'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(position, player)
                reply_text += ' - geschatte tijd tot afspelen: %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, time_until)
        em = discord.Embed(title=entry.title, description=waitlist_text, colour=0xDEADBF)
        em.set_author(name=author,icon_url=author.avatar_url)
        entry_name = urllib.parse.quote(entry.title)
        spotify_request = 'https://api.spotify.com/v1/search?q={0}&type=track&limit=1'.format(entry_name)
        json = await self.get_json_data(spotify_request)
        if song_url.startswith('youtu.be') or song_url.startswith('youtube.com'):
            em.set_footer(text='https://' + song_url)
        elif song_url.startswith('https://www.youtube.com') or song_url.startswith('https://youtu.be'):
            em.set_footer(text=song_url)
        elif song_url.startswith('https://') or song_url.startswith('http://'):
            em.set_footer(text=song_url)
        else:
            em.set_footer(text='https://youtu.be/' + song_url)
        try:
            tracks = json.get("tracks", None)
            items = tracks.get("items", None)
            images = items[0]['album']['images']
            image_url = images[0]['url']
        except Exception as e:
            if song_url.startswith('https://www.youtube.com') or song_url.startswith('youtube.com'):
                youtubeurl_parse = song_url.rsplit('=',1)[1]
                print(youtubeurl_parse)
                print(youtubeurl_parse[0])
                youtubeurl = ('http://img.youtube.com/vi/'+ youtubeurl_parse +'/mqdefault.jpg')
                print(youtubeurl)
                image_url = youtubeurl
            elif song_url.startswith('https://www.youtu.be') or song_url.startswith('youtu.be'):
                youtubeurl_parse = song_url.rsplit('/', 1)[1]
                print(youtubeurl_parse)
                print(youtubeurl_parse[0])
                youtubeurl = ('http://img.youtube.com/vi/' + youtubeurl_parse + '/mqdefault.jpg')
                print(youtubeurl)
                image_url = youtubeurl
            else:
                youtube_url = ('http://img.youtube.com/vi/' + song_url + '/mqdefault.jpg')
                image_url = youtube_url

        em.set_thumbnail(url=image_url)
        message = await self.send_message(channel, embed=em)
        await asyncio.sleep(30)
        await self.safe_delete_message(message)
        return Response("🚮", delete_after=1)

    cmd_p = cmd_play

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("Die afspeellijst kan niet worden gespeeld.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "%s nummers verwerken..." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Fout bij het in de wachtrij zetten van afspeellijst %s.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Fout bij het in de wachtrij zetten van afspeellijst %s.' % playlist_url, expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                print("Verwijderde %s nummers" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        print("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            self._fixg(ttime),
            ttime / num_songs,
            ttime / num_songs - wait_per_song,
            self._fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "Geen nummers toegevoegd, alle nummers waren te lang (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nBovendien is het huidige nummer overgeslagen omdat het te lang is."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("{} nummers in wachtrij die over {} seconden worden afgespeeld".format(
            songs_added, self._fixg(ttime, 1)), delete_after=30)

    async def cmd_search(self, player, channel, author, permissions, leftover_args):
        """
        Uitleg:
            ;search [website] [nummer] zoekopdracht

        Doorzoekt een website naar een video
        - service: een van de volgende diensten:
            - youtube (yt) (standaard indien niet opgegeven)
            - soundcloud (sc)
            - yahoo (yh) (waarom yahoo, het is huidig jaar)
        - nummer: geef aan hoeveel video's je wilt beoordelen:
            - standaard is 1 als je niets aangeeft
            - opmerking: Als je zoekopdracht start met een nummer dient die in aanhalingstekens te zijn
            - voorbeeld: ;search 2 "2 vingers in de lucht, kom op"
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                "Je hebt je limiet voor maximum aantal nummers in playlist bereikt (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    "Geef alsjeblieft een zoekterm op.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("Voer alsjeblieft de zoekterm juist in.", expire_in=30)

        service = 'youtube'
        items_requested = 5
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("Je kan niet naar meer dan %s video's zoeken" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Zoekt naar video's...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("Geen video's gevonden.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            confirm_message = await self.safe_send_message(channel, "Is dit goed? Type `y`, `n` of `exit`")
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("Okee, dan niet.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

                await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

                return Response("Prima, komt er nu aan!", delete_after=30)
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Ach ja, :frowning:", delete_after=30)

    async def cmd_np(self, player, channel, server, message):
        """
        Uitleg:
            ;np

        Laat zien welk nummer nu gespeeld wordt.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "Speelt nu af: **%s** toegevoegd door **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
            else:
                np_text = "Speelt nu af: **%s** %s\n" % (player.current_entry.title, prog_str)

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                'Er staan geen nummers in de wachtrij. Voeg iets toe met ;play.'.format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, author, voice_channel):
        """
        Uitleg:
            ;summon

        Roep de bot op om naar je spraakkanaal te komen.
        """

        if not author.voice_channel:
            raise exceptions.CommandError('Je zit niet in een spraakkanaal!')

        voice_client = self.the_voice_clients.get(channel.server.id, None)
        if voice_client and voice_client.channel.server == author.voice_channel.server:
            await self.move_voice_client(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

        if not chperms.connect:
            self.safe_print("Kan niet in kanaal \"%s\" komen; geen permissies." % author.voice_channel.name)
            return Response(
                "```Kan niet in kanaal \"%s\" komen; geen permissies.```" % author.voice_channel.name,
                delete_after=25
            )

        elif not chperms.speak:
            self.safe_print("Kan niet in kanaal \"%s\" komen, geen permissie om te spreken." % author.voice_channel.name)
            return Response(
                "```Kan niet in kanaal \"%s\" komen, geen permissie om te spreken.```" % author.voice_channel.name,
                delete_after=25
            )

        player = await self.get_player(author.voice_channel, create=True)

        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)

    async def cmd_pause(self, player):
        """
        Uitleg:
            ;pause

        Pauzeert het nummer dat nu speelt.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError('Muziek speelt nu niet.', expire_in=30)

    async def cmd_resume(self, player):
        """
        Uitleg:
            ;resume

        Gaat verder met het spelen van dit nummer.
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError('Muziek is nu niet gepauseerd.', expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Uitleg:
            ;shuffle

        Zet de wachtrij in willekeurige volgorde.
        """

        player.playlist.shuffle()

        cards = [':spades:',':clubs:',':hearts:',':diamonds:']
        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(":ok_hand:", delete_after=15)

    async def cmd_clear(self, player, author):
        """
        Uitleg:
            ;clear

        Maakt de huidige wachtrij leeg.
        """

        player.playlist.clear()
        return Response(':put_litter_in_its_place:', delete_after=20)

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
        """
        Uitleg:
            ;skip

        Slaat het nummer dat speelt over als er genoeg stemmen zijn, of wanneer een moderator dit commando gebruikt.
        """

        if player.is_stopped:
            raise exceptions.CommandError("Er is niets om over te slaan, want er spelen geen nummers.", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    # print(player.playlist.peek()._waiting_futures[0].__dict__)
                    em2 = discord.Embed(title=message,description=message,colour=0xDEADBF)
                    return Response("Het volgende nummer (%s) is aan het downloaden. Een ogenblik geduld alsjeblieft." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("Het volgende nummer wordt spoedig afgespeeld. Een ogenblik geduld alsjeblieft.")
                else:
                    print("Er gebeurt iets vreemds.  "
                          "Misschien moet ik herstart worden. Vraag naar de beheerders of de moderators.")
            else:
                print("Er gebeurt iets vreemds.  "
                          "Misschien moet ik herstart worden. Vraag naar de beheerders of de moderators.")

        if author.id == self.config.owner_id \
                or permissions.instaskip \
                or author == player.current_entry.meta.get('author', None):

            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(self.config.skips_required,
                              sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'Je stem om **{}** over te slaan was bevestigd.'
                '\nDe hoeveelheid stemmen om het nummer over te slaan is gehaald.{}'.format(
                    player.current_entry.title,
                    ' Volgende nummer komt eraan!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            
            return Response(
                'Je stem om **{}** over te slaan was bevestigd.'
                '\n**{}** {} nog nodig om het nummer over te slaan.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'stemmen' if skips_remaining == 1 else 'stem'
                ),
                reply=True,
                delete_after=20
            )

    cmd_opgekankerd = cmd_skip
    async def cmd_voteskip(self, player, channel, author, message, permissions, voice_channel):
        """
        Uitleg:
            ;voteskip

        Slaat het nummer dat nu speelt over als er genoeg stemmen zijn.
        """

        if player.is_stopped:
            raise exceptions.CommandError("Er is niets om over te slaan, want er spelen geen nummers.", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    # print(player.playlist.peek()._waiting_futures[0].__dict__)
                    return Response("Het volgende nummer (%s) is aan het downloaden. Een ogenblik geduld alsjeblieft." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("Het volgende nummer wordt spoedig afgespeeld. Een ogenblik geduld alsjeblieft.")
                else:
                    print("Er gebeurt iets vreemds.  "
                          "Misschien moet ik herstart worden. Vraag naar de beheerders of de moderators.")
            else:
                print("Er gebeurt iets vreemds.  "
                          "Misschien moet ik herstart worden. Vraag naar de beheerders of de moderators.")

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(self.config.skips_required,
                              sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'Je stem om **{}** over te slaan was bevestigd.'
                '\nDe hoeveelheid stemmen om het nummer over te slaan is gehaald.{}'.format(
                    player.current_entry.title,
                    ' Volgende nummer komt eraan!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'Je stem om **{}** over te slaan was bevestigd.'
                '\n**{}** {} nog nodig om het nummer over te slaan.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'stem' if skips_remaining == 1 else 'stemmen'
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, message, player, new_volume=None):
        """
        Uitleg:
            ;volume (+/-)[volume]

        Stelt het volume in. Waardes van 1 tot 100.
        Met een + of - voor het getal maak je de toevoeging relatief.
        """

        if not new_volume:
            return Response('Huidig volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError('{} is geen geldig getal'.format(new_volume), expire_in=20)

        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('Volume aangepast van %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    'Onmogelijke verandering van volume: {}{:+} -> {}%. Geef een verandering tussen {} en {:+} op.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    'Onmogelijke verandering van volume: {}%. Geef een waarde tussen 0 en 100 op.'.format(new_volume), expire_in=20)

    async def cmd_queue(self, channel, player, author):
        """
        Uitleg:
            ;queue

        Geeft de huidige wachtrij weer.
        """

        lines = []
        now_playing_queue = 'Er staan geen nummers in de wachtrij! Voeg iets toe met ;play.'
        queuelist = []
        queue_message = "Er staan geen nummers in de wachtrij!"
        unlisted = 0
        andmoretext = '* ... en %s meer*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                now_playing_queue = ("**%s** toegevoegd door **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
                title = 'Speelt nu:'
            else:
                lines.append("Speelt nu: **%s** %s\n" % (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = '`{}.` **{}** toegevoegd door **{}**'.format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = '`{}.` **{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue
            queuelist.append(nextline)
            queue_message = '\n'.join(queuelist)

        if unlisted:
            lines.append('\n*... en %s meer*' % unlisted)

        if not lines:
            lines.append(
                'Er staan geen nummers in de wachtrij! Voeg iets toe met ;play.'.format(self.config.command_prefix))
            title = 'Wachtrij:'

        message = '\n'.join(lines)
        if now_playing_queue is not "Er staan geen nummers in de wachtrij! Voeg iets toe met ;play.":
            title = 'Speelt nu:'
        em1 = discord.Embed(title=title, description=now_playing_queue, colour = 0xDEADBF)
        if title == 'Speelt nu:':
            em1.add_field(name='Wachtrij: ',value=queue_message)
        em1.set_author(name=author, icon_url=author.avatar_url)
        discord_message = await self.send_message(channel, embed=em1)
        await asyncio.sleep(30)
        await self.safe_delete_message(discord_message)
        return Response("🚮", delete_after=1)

    # alias for 'queue'
    cmd_q = cmd_queue

    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        Uitleg:
            ;clean [hoeveelheid]

        Verwijdert [hoeveelheid] berichten die de bot heeft geplaatst. Standaard: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("Voer een nummer in. Cijfers, dus. Snap je?", reply=True, delete_after=8)
            # heb je snarkiness een beetje informatiever gemaakt, lol --Tony

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('{} bericht{} opgeruimd.'.format(len(deleted), 'en' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('{} bericht{} opgeruimd.'.format(deleted, 'en' * bool(deleted)), delete_after=15)

    async def cmd_pldump(self, channel, song_url):
        """
        Gebruik:
            ;pldump url

        Geeft een overzicht van de individuele URLs van een afspeellijst.
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("Could not extract info from input url\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("Could not extract info from input url, no data.", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("This does not seem to be a playlist.", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("Could not extract info from input url, unsupported playlist type.", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await self.send_file(channel, fcontent, filename='playlist.txt', content="Here's the url dump for <%s>" % song_url)

        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            ;listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response(":mailbox_with_mail:", delete_after=20)


    async def cmd_perms(self, author, channel, server, permissions):
        """
        Uitleg:
            ;perms

        Stuurt de gebruiker een lijst van zijn permissies.
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response(":mailbox_with_mail:", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            ;setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            ;setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            ;setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: %s" % e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)


    async def cmd_disconnect(self, server):
        """
        Uitleg:
            ;disconnect

        Haalt de bot uit het spraakkanaal.
        Alleen voor beheerders en moderators.
        """
        await self.disconnect_voice_client(server)
        return Response(":hear_no_evil:", delete_after=20)

    async def cmd_restart(self, channel):
        """
        Uitleg:
            ;restart

        Herstart de bot.
        Alleen voor beheerders en moderators.
        Helaas is het soms nodig.
        """
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal

    async def cmd_shutdown(self, channel):
        """
        Uitleg:
            ;shutdown

        Zet de bot uit.
        Alleen voor de eigenaar (Auxim).
        """
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal

    async def cmd_author(self, message, channel):
        """
        Uitleg:
            ;author

        Wie de persoon is die je onder moet spammen als de bot offline gaat.
        """
        return Response ("Deze bot wordt gedraaid en onderhouden door Auxim. Met hulp van mijn maat Pim. https://github.com/GeoffreyWesthoff/DJWillex", delete_after=20)

    async def cmd_spooky(self, message, channel):
        """
        boo
        """
        message = await self.send_message(channel, ";play spooky scary skeletoons the living tombstone") #spooky
        await self.delete_message(message)
        return Response(":ghost:", delete_after=20)

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return  # if I want to log this I just move it under the prefix check

        command, *args = message_content.split()  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'cmd_%s' % command, None)
        if not handler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.send_message(message.channel, 'Je kan deze bot niet gebruiken in een privé bericht.')
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            self.safe_print("[Gebruiker geblokkeerd] {0.id}/{0.name} ({1})".format(message.author, message_content))
            return

        else:
            self.safe_print("[Commando] {0.id}/{0.name} ({1})".format(message.author, message_content))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "Dit commando is niet ingeschakeld voor jouw groep (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "Dit commando is verboden voor jouw groep (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Gebruik: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            print("{0.__class__}: {0.message}".format(e))

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n%s\n```' % e.message,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def on_voice_state_update(self, before, after):
        if not all([before, after]):
            return

        if before.voice_channel == after.voice_channel:
            return

        if before.server.id not in self.players:
            return

        my_voice_channel = after.server.me.voice_channel  # This should always work, right?

        if not my_voice_channel:
            return

        if before.voice_channel == my_voice_channel:
            joining = False
        elif after.voice_channel == my_voice_channel:
            joining = True
        else:
            return  # Not my channel

        moving = before == before.server.me

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(my_voice_channel)

        if after == after.server.me and after.voice_channel:
            player.voice_client.channel = after.voice_channel

        if not self.config.auto_pause:
            return

        if sum(1 for m in my_voice_channel.voice_members if m != after.server.me):
            if auto_paused and player.is_paused:
                print("[config:autopause] Verder gaan")
                self.server_specific_data[after.server]['auto_paused'] = False
                player.resume()
        else:
            if not auto_paused and player.is_playing:
                print("[config:autopause] Pauzeren")
                self.server_specific_data[after.server]['auto_paused'] = True
                player.pause()

    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            self.safe_print("[Servers] \"%s\" veranderden van regio: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)



if __name__ == '__main__':
    bot = MusicBot()
    bot.run()
