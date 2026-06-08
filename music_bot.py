import discord
from discord.ext import commands
import yt_dlp
import asyncio
from async_timeout import timeout
from collections import deque
from config import TOKEN

# Setup bot intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration options for yt-dlp
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

class Track:
    def __init__(self, title, query):
        self.title = title
        self.query = query

class MusicPlayer:
    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = deque()       
        self.history = deque()     
        self.current = None        
        self.next_event = asyncio.Event()
        self.is_moving_back = False 
        self.is_switching = False  # Prevents overlapping stream loads during batch operations
        self.repeat_one = False
        self.repeat_all = False

        self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next_event.clear()

            while len(self.queue) == 0:
                await asyncio.sleep(0.5)

            # If a rapid batch sequence is manipulating deques, pause briefly
            if self.is_switching:
                await asyncio.sleep(0.2)
                continue

            track_to_play = self.queue.popleft()
            
            async with self._channel.typing():
                try:
                    source = await YTDLSource.from_url(track_to_play.query, loop=self.bot.loop, stream=True)
                except Exception as e:
                    await self._channel.send(f"Could not load track '{track_to_play.title}': {e}")
                    continue

            if self.current and not self.is_moving_back:
                self.history.append(self.current) 
            
            self.current = track_to_play
            self.is_moving_back = False 

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next_event.set))
            await self._channel.send(f'**Now playing:** {track_to_play.title}')
            
            await self.next_event.wait()

            if self.repeat_one:
                self.queue.appendleft(self.current)
            elif self.repeat_all:
                self.queue.append(self.current)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def get_player(self, ctx):
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
        return player

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass
        try:
            del self.players[guild.id]
        except KeyError:
            pass

    @commands.command(name='join')
    async def join(self, ctx):
        if not ctx.author.voice:
            return await ctx.send(f"{ctx.author.mention}, join a voice channel first!")
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()

    @commands.command(name='play', aliases=['add'])
    async def play(self, ctx, *, search: str):
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        async with ctx.typing():
            try:
                data = await self.bot.loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))
                if 'entries' in data:
                    data = data['entries'][0]
                track = Track(title=data['title'], query=data['webpage_url'])
            except Exception as e:
                return await ctx.send(f"Error fetching metadata for '{search}': {e}")

            player = self.get_player(ctx)
            player.queue.append(track)
            
            if ctx.voice_client.is_playing() or len(player.queue) > 1:
                await ctx.send(f'Added to queue: **{track.title}**')

    @commands.command(name='skip', aliases=['next'])
    async def skip(self, ctx):
        if not ctx.voice_client:
            return await ctx.send("Not connected to a voice channel.")

        player = self.get_player(ctx)
        if len(player.queue) == 0 and not ctx.voice_client.is_playing():
            return await ctx.send("Nothing left to skip!")

        player.is_switching = True

        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        else:
            if player.current:
                player.history.append(player.current)
                player.current = None
            player.next_event.set()

        player.is_switching = False
        await ctx.send("Skipped current track.")

    @commands.command(name='previous', aliases=['prev'])
    async def previous(self, ctx):
        player = self.get_player(ctx)
        if len(player.history) == 0:
            return await ctx.send("There is no music history to move backward to!")

        player.is_switching = True

        prev_track = player.history.pop()
        if player.current:
            player.queue.appendleft(player.current)

        player.queue.appendleft(prev_track)
        player.is_moving_back = True 

        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        else:
            player.current = None
            player.next_event.set()

        player.is_switching = False
        await ctx.send(f"Moving back to: **{prev_track.title}**")

    @commands.command(name='queue', aliases=['q'])
    async def view_queue(self, ctx):
        player = self.get_player(ctx)
        if len(player.queue) == 0 and not player.current:
            return await ctx.send("The queue is currently empty.")

        upcoming = list(player.queue)
        current_str = f"**Now Playing:** {player.current.title}\n\n" if player.current else ""
        queue_str = "\n".join(f"**{i+1}.** {track.title}" for i, track in enumerate(upcoming[:10]))
        
        embed = discord.Embed(title="Active Music Timeline", description=current_str + (queue_str if queue_str else "*No upcoming tracks queued.*"), color=discord.Color.blurple())
        await ctx.send(embed=embed)

    @commands.command(name="repeat")
    async def repeat(self, ctx, mode: str = None):
        """
        !repeat off  -> disable repeat
        !repeat one  -> repeat current track
        !repeat all  -> loop queue
        """
        player = self.get_player(ctx)

        if mode is None:
            status = (
                "off"
                if not player.repeat_one and not player.repeat_all
                else "one" if player.repeat_one
                else "all"
            )
            return await ctx.send(f"Repeat mode is currently: **{status}**")

        mode = mode.lower()
        if mode == "off":
            player.repeat_one = False
            player.repeat_all = False
            await ctx.send("Repeat mode **disabled**.")
        elif mode == "one":
            player.repeat_one = True
            player.repeat_all = False
            await ctx.send("Repeat mode set to: **repeat current track**.")
        elif mode == "all":
            player.repeat_one = False
            player.repeat_all = True
            await ctx.send("Repeat mode set to: **loop queue**.")
        else:
            await ctx.send("Invalid mode. Use `!repeat off`, `!repeat one`, or `!repeat all`.")
    
    @commands.command(name='stop')
    async def stop(self, ctx):
        await self.cleanup(ctx.guild)
        await ctx.send("Wiped layout queue and disconnected.")


# --- GLOBAL BATCH INTERCEPTOR ---
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.content.startswith('!'):
        if ',' in message.content:
            content_without_prefix = message.content[1:]
            raw_instructions = [inst.strip() for inst in content_without_prefix.split(',')]

            await message.channel.send(f"List detected! Running `{len(raw_instructions)}` sequential operations...")

            current_action = "play" 

            for item in raw_instructions:
                if item.startswith('!'):
                    item = item[1:]

                parts = item.split(' ', 1)
                keyword = parts[0].lower()
                args = parts[1] if len(parts) > 1 else None

                if keyword in ['play', 'add', 'skip', 'next', 'previous', 'prev', 'queue', 'q', 'stop', 'join']:
                    current_action = keyword
                else:
                    args = item 
                    keyword = current_action

                full_command_string = f"!{keyword} {args}" if args else f"!{keyword}"
                
                new_msg = copy_message_with_content(message, full_command_string)
                await bot.process_commands(new_msg)

                await asyncio.sleep(1.5)

            await message.channel.send("List operations completed.")
            return

    await bot.process_commands(message)

def copy_message_with_content(msg, new_content):
    import copy
    copied = copy.copy(msg)
    copied.content = new_content
    return copied

async def setup_hook():
    await bot.add_cog(Music(bot))

bot.setup_hook = setup_hook

bot.run(TOKEN)