from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-milestones")

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "voice_milestones.sqlite3"))
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0")) or None

HOUR = 60 * 60
DAY = 24 * HOUR


@dataclass(frozen=True)
class Milestone:
    number: int
    label: str
    seconds: int


# Les mois sont volontairement fixés à 30 jours et l'année à 365 jours.
DEFAULT_MILESTONES: tuple[Milestone, ...] = (
    Milestone(1, "1 heure", 1 * HOUR),
    Milestone(2, "6 heures", 6 * HOUR),
    Milestone(3, "12 heures", 12 * HOUR),
    Milestone(4, "1 jour", 1 * DAY),
    Milestone(5, "2 jours", 2 * DAY),
    Milestone(6, "3 jours", 3 * DAY),
    Milestone(7, "1 semaine", 7 * DAY),
    Milestone(8, "2 semaines", 14 * DAY),
    Milestone(9, "1 mois", 30 * DAY),
    Milestone(10, "2 mois", 60 * DAY),
    Milestone(11, "3 mois", 90 * DAY),
    Milestone(12, "4 mois", 120 * DAY),
    Milestone(13, "5 mois", 150 * DAY),
    Milestone(14, "6 mois", 180 * DAY),
    Milestone(15, "7 mois", 210 * DAY),
    Milestone(16, "8 mois", 240 * DAY),
    Milestone(17, "9 mois", 270 * DAY),
    Milestone(18, "10 mois", 300 * DAY),
    Milestone(19, "11 mois", 330 * DAY),
    Milestone(20, "1 an", 365 * DAY),
)

DEFAULT_MILESTONE_BY_NUMBER = {
    milestone.number: milestone for milestone in DEFAULT_MILESTONES
}

DEFAULT_MILESTONE_MESSAGE = (
    "Félicitations {membre} !\n"
    "Tu as atteint **{duree}** de temps vocal actif."
)

DEFAULT_ROLE_MESSAGE = (
    "Tu reçois automatiquement le rôle {role} pour avoir atteint "
    "le palier **{palier}**."
)

MESSAGE_PLACEHOLDERS = "{membre}, {palier}, {duree}, {temps}, {role}"


def format_duration(total_seconds: int) -> str:
    """Format compact utilisé pour afficher le temps d'un membre."""
    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, DAY)
    hours, remainder = divmod(remainder, HOUR)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} j")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    if not days and not hours and minutes < 1:
        parts.append(f"{seconds} s")

    return " ".join(parts)


def format_milestone_duration(total_seconds: int) -> str:
    """Format lisible utilisé pour le nom des paliers configurables."""
    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, DAY)
    hours, remainder = divmod(remainder, HOUR)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} jour{'s' if days > 1 else ''}")
    if hours:
        parts.append(f"{hours} heure{'s' if hours > 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if seconds and not parts:
        parts.append(f"{seconds} seconde{'s' if seconds > 1 else ''}")

    return " ".join(parts) or "0 seconde"


class VoiceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                announcement_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS voice_totals (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                total_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS voice_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL,
                seconds INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_voice_segments_guild_end
                ON voice_segments (guild_id, ended_at);

            CREATE TABLE IF NOT EXISTS announced_milestones (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                announced_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, milestone)
            );

            CREATE TABLE IF NOT EXISTS milestone_roles (
                guild_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('add', 'remove')),
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, milestone, action)
            );

            CREATE TABLE IF NOT EXISTS milestone_times (
                guild_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                seconds INTEGER NOT NULL,
                PRIMARY KEY (guild_id, milestone)
            );

            CREATE TABLE IF NOT EXISTS milestone_messages (
                guild_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                milestone_message TEXT,
                role_message TEXT,
                PRIMARY KEY (guild_id, milestone)
            );
            """
        )
        # Migration des anciennes bases : un ancien rôle associé à un palier
        # reste un rôle à ajouter. La nouvelle clé permet une action "add"
        # et une action "remove" simultanées pour le même palier.
        cursor = await self.connection.execute("PRAGMA table_info(milestone_roles)")
        role_columns = await cursor.fetchall()
        has_action = any(str(row[1]) == "action" for row in role_columns)
        primary_key_columns = [
            str(row[1])
            for row in sorted(role_columns, key=lambda row: int(row[5]))
            if int(row[5]) > 0
        ]

        if not has_action or primary_key_columns != ["guild_id", "milestone", "action"]:
            await self.connection.execute(
                """
                CREATE TABLE milestone_roles_new (
                    guild_id INTEGER NOT NULL,
                    milestone INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('add', 'remove')),
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, milestone, action)
                )
                """
            )
            if has_action:
                await self.connection.execute(
                    """
                    INSERT OR REPLACE INTO milestone_roles_new
                        (guild_id, milestone, action, role_id)
                    SELECT guild_id, milestone, action, role_id
                    FROM milestone_roles
                    """
                )
            else:
                await self.connection.execute(
                    """
                    INSERT OR REPLACE INTO milestone_roles_new
                        (guild_id, milestone, action, role_id)
                    SELECT guild_id, milestone, 'add', role_id
                    FROM milestone_roles
                    """
                )
            await self.connection.execute("DROP TABLE milestone_roles")
            await self.connection.execute(
                "ALTER TABLE milestone_roles_new RENAME TO milestone_roles"
            )

        await self.connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()

    async def set_announcement_channel(self, guild_id: int, channel_id: int) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO guild_config (guild_id, announcement_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    announcement_channel_id = excluded.announcement_channel_id
                """,
                (guild_id, channel_id),
            )
            await self.connection.commit()

    async def get_announcement_channel(self, guild_id: int) -> Optional[int]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                "SELECT announcement_channel_id FROM guild_config WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            if row is None or row["announcement_channel_id"] is None:
                return None
            return int(row["announcement_channel_id"])

    async def set_role(
        self,
        guild_id: int,
        milestone: int,
        action: str,
        role_id: int,
    ) -> None:
        assert self.connection is not None
        if action not in {"add", "remove"}:
            raise ValueError("Action de rôle invalide.")

        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO milestone_roles (guild_id, milestone, action, role_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, milestone, action) DO UPDATE SET
                    role_id = excluded.role_id
                """,
                (guild_id, milestone, action, role_id),
            )
            await self.connection.commit()

    async def remove_role(
        self,
        guild_id: int,
        milestone: int,
        action: Optional[str] = None,
    ) -> None:
        assert self.connection is not None
        async with self.lock:
            if action is None:
                await self.connection.execute(
                    "DELETE FROM milestone_roles WHERE guild_id = ? AND milestone = ?",
                    (guild_id, milestone),
                )
            else:
                await self.connection.execute(
                    """
                    DELETE FROM milestone_roles
                    WHERE guild_id = ? AND milestone = ? AND action = ?
                    """,
                    (guild_id, milestone, action),
                )
            await self.connection.commit()

    async def get_roles(self, guild_id: int, milestone: int) -> dict[str, int]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT action, role_id
                FROM milestone_roles
                WHERE guild_id = ? AND milestone = ?
                """,
                (guild_id, milestone),
            )
            rows = await cursor.fetchall()
            return {str(row["action"]): int(row["role_id"]) for row in rows}

    async def get_all_roles(self, guild_id: int) -> dict[int, dict[str, int]]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT milestone, action, role_id
                FROM milestone_roles
                WHERE guild_id = ?
                ORDER BY milestone, action
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()

        result: dict[int, dict[str, int]] = {}
        for row in rows:
            result.setdefault(int(row["milestone"]), {})[str(row["action"])] = int(
                row["role_id"]
            )
        return result

    async def get_milestones(self, guild_id: int) -> tuple[Milestone, ...]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT milestone, seconds
                FROM milestone_times
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()

        configured_seconds = {
            int(row["milestone"]): int(row["seconds"])
            for row in rows
        }

        return tuple(
            Milestone(
                number=default.number,
                label=format_milestone_duration(
                    configured_seconds.get(default.number, default.seconds)
                ),
                seconds=configured_seconds.get(default.number, default.seconds),
            )
            for default in DEFAULT_MILESTONES
        )

    async def set_milestone_time(
        self,
        guild_id: int,
        milestone: int,
        seconds: int,
    ) -> None:
        assert self.connection is not None
        if milestone not in DEFAULT_MILESTONE_BY_NUMBER:
            raise ValueError("Numéro de palier invalide.")
        if seconds <= 0:
            raise ValueError("La durée doit être supérieure à zéro.")

        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO milestone_times (guild_id, milestone, seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, milestone) DO UPDATE SET
                    seconds = excluded.seconds
                """,
                (guild_id, milestone, seconds),
            )
            await self.connection.commit()

    async def reset_milestone_time(self, guild_id: int, milestone: int) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute(
                """
                DELETE FROM milestone_times
                WHERE guild_id = ? AND milestone = ?
                """,
                (guild_id, milestone),
            )
            await self.connection.commit()

    async def reset_all_milestone_times(self, guild_id: int) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute(
                "DELETE FROM milestone_times WHERE guild_id = ?",
                (guild_id,),
            )
            await self.connection.commit()

    async def set_milestone_message(
        self,
        guild_id: int,
        milestone: int,
        message: str,
    ) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO milestone_messages
                    (guild_id, milestone, milestone_message)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, milestone) DO UPDATE SET
                    milestone_message = excluded.milestone_message
                """,
                (guild_id, milestone, message),
            )
            await self.connection.commit()

    async def set_role_message(
        self,
        guild_id: int,
        milestone: int,
        message: str,
    ) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO milestone_messages
                    (guild_id, milestone, role_message)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, milestone) DO UPDATE SET
                    role_message = excluded.role_message
                """,
                (guild_id, milestone, message),
            )
            await self.connection.commit()

    async def get_messages(self, guild_id: int, milestone: int) -> tuple[str, str]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT milestone_message, role_message
                FROM milestone_messages
                WHERE guild_id = ? AND milestone = ?
                """,
                (guild_id, milestone),
            )
            row = await cursor.fetchone()

        milestone_message = (
            str(row["milestone_message"])
            if row and row["milestone_message"]
            else DEFAULT_MILESTONE_MESSAGE
        )
        role_message = (
            str(row["role_message"])
            if row and row["role_message"]
            else DEFAULT_ROLE_MESSAGE
        )
        return milestone_message, role_message

    async def reset_milestone_message(
        self, guild_id: int, milestone: int, message_type: str
    ) -> None:
        assert self.connection is not None
        column = "milestone_message" if message_type == "palier" else "role_message"
        async with self.lock:
            await self.connection.execute(
                f"""
                UPDATE milestone_messages
                SET {column} = NULL
                WHERE guild_id = ? AND milestone = ?
                """,
                (guild_id, milestone),
            )
            await self.connection.commit()

    async def begin_session(
        self,
        guild_id: int,
        user_id: int,
        now: Optional[int] = None,
    ) -> None:
        assert self.connection is not None
        now = now or int(time.time())
        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO active_sessions (guild_id, user_id, started_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (guild_id, user_id, now),
            )
            await self.connection.commit()

    async def end_or_checkpoint_session(
        self,
        guild_id: int,
        user_id: int,
        *,
        keep_active: bool,
        now: Optional[int] = None,
    ) -> tuple[int, int]:
        """Retourne (ancien_total, nouveau_total)."""
        assert self.connection is not None
        now = now or int(time.time())

        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT started_at
                FROM active_sessions
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            session = await cursor.fetchone()

            cursor = await self.connection.execute(
                """
                SELECT total_seconds
                FROM voice_totals
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            old_total = int(row["total_seconds"]) if row else 0

            if session is None:
                return old_total, old_total

            session_started_at = int(session["started_at"])
            elapsed = max(0, now - session_started_at)
            new_total = old_total + elapsed

            if elapsed > 0:
                await self.connection.execute(
                    """
                    INSERT INTO voice_segments
                        (guild_id, user_id, started_at, ended_at, seconds)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (guild_id, user_id, session_started_at, now, elapsed),
                )

            await self.connection.execute(
                """
                INSERT INTO voice_totals (guild_id, user_id, total_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    total_seconds = excluded.total_seconds
                """,
                (guild_id, user_id, new_total),
            )

            if keep_active:
                await self.connection.execute(
                    """
                    UPDATE active_sessions
                    SET started_at = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (now, guild_id, user_id),
                )
            else:
                await self.connection.execute(
                    """
                    DELETE FROM active_sessions
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild_id, user_id),
                )

            await self.connection.commit()
            return old_total, new_total

    async def get_total(
        self,
        guild_id: int,
        user_id: int,
        include_active: bool = True,
    ) -> int:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT total_seconds
                FROM voice_totals
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            total = int(row["total_seconds"]) if row else 0

            if include_active:
                cursor = await self.connection.execute(
                    """
                    SELECT started_at
                    FROM active_sessions
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild_id, user_id),
                )
                session = await cursor.fetchone()
                if session:
                    total += max(0, int(time.time()) - int(session["started_at"]))

            return total


    async def set_member_total(
        self,
        guild_id: int,
        user_id: int,
        total_seconds: int,
        now: Optional[int] = None,
    ) -> None:
        """Remplace manuellement le temps vocal total d'un membre."""
        assert self.connection is not None

        total_seconds = max(0, int(total_seconds))
        now = now or int(time.time())

        async with self.lock:
            await self.connection.execute(
                """
                INSERT INTO voice_totals (
                    guild_id,
                    user_id,
                    total_seconds
                )
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    total_seconds = excluded.total_seconds
                """,
                (guild_id, user_id, total_seconds),
            )

            # Si le membre est actuellement en vocal,
            # sa session repart à partir de maintenant.
            await self.connection.execute(
                """
                UPDATE active_sessions
                SET started_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (now, guild_id, user_id),
            )

            await self.connection.commit()


    async def clear_active_sessions(self) -> None:
        assert self.connection is not None
        async with self.lock:
            await self.connection.execute("DELETE FROM active_sessions")
            await self.connection.commit()

    async def mark_milestone_announced(
        self,
        guild_id: int,
        user_id: int,
        milestone: int,
    ) -> bool:
        """Retourne True uniquement si le palier n'avait jamais été annoncé."""
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO announced_milestones
                    (guild_id, user_id, milestone, announced_at)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, milestone, int(time.time())),
            )
            await self.connection.commit()
            return cursor.rowcount > 0


    async def reset_member_stats(self, guild_id: int, user_id: int) -> None:
        """Supprime toutes les statistiques vocales d'un membre."""
        assert self.connection is not None
        async with self.lock:
            for table in (
                "voice_totals",
                "active_sessions",
                "voice_segments",
                "announced_milestones",
            ):
                await self.connection.execute(
                    f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            await self.connection.commit()

    async def reset_guild_stats(self, guild_id: int) -> None:
        """Supprime toutes les statistiques vocales d'un serveur."""
        assert self.connection is not None
        async with self.lock:
            for table in (
                "voice_totals",
                "active_sessions",
                "voice_segments",
                "announced_milestones",
            ):
                await self.connection.execute(
                    f"DELETE FROM {table} WHERE guild_id = ?",
                    (guild_id,),
                )
            await self.connection.commit()

    async def all_totals(self, guild_id: int) -> list[tuple[int, int]]:
        assert self.connection is not None
        async with self.lock:
            cursor = await self.connection.execute(
                """
                SELECT user_id, total_seconds
                FROM voice_totals
                WHERE guild_id = ?
                """,
                (guild_id,),
            )
            rows = await cursor.fetchall()
            return [
                (int(row["user_id"]), int(row["total_seconds"]))
                for row in rows
            ]


    async def get_leaderboard(
        self,
        guild_id: int,
        start_timestamp: Optional[int] = None,
        now: Optional[int] = None,
    ) -> list[tuple[int, int]]:
        """Retourne le classement trié sous la forme (user_id, secondes).

        Pour une période limitée, seuls les segments enregistrés depuis
        l'installation de cette version peuvent être comptabilisés.
        """
        assert self.connection is not None
        now = now or int(time.time())

        async with self.lock:
            if start_timestamp is None:
                cursor = await self.connection.execute(
                    """
                    SELECT vt.user_id,
                           vt.total_seconds + COALESCE(
                               MAX(0, ? - a.started_at), 0
                           ) AS seconds
                    FROM voice_totals AS vt
                    LEFT JOIN active_sessions AS a
                      ON a.guild_id = vt.guild_id
                     AND a.user_id = vt.user_id
                    WHERE vt.guild_id = ?
                    GROUP BY vt.user_id

                    UNION ALL

                    SELECT a.user_id, MAX(0, ? - a.started_at) AS seconds
                    FROM active_sessions AS a
                    LEFT JOIN voice_totals AS vt
                      ON vt.guild_id = a.guild_id
                     AND vt.user_id = a.user_id
                    WHERE a.guild_id = ? AND vt.user_id IS NULL

                    ORDER BY seconds DESC
                    """,
                    (now, guild_id, now, guild_id),
                )
            else:
                cursor = await self.connection.execute(
                    """
                    WITH recorded AS (
                        SELECT user_id,
                               SUM(
                                   MAX(
                                       0,
                                       MIN(ended_at, ?) - MAX(started_at, ?)
                                   )
                               ) AS seconds
                        FROM voice_segments
                        WHERE guild_id = ?
                          AND ended_at > ?
                          AND started_at < ?
                        GROUP BY user_id
                    ),
                    current_sessions AS (
                        SELECT user_id,
                               MAX(0, ? - MAX(started_at, ?)) AS seconds
                        FROM active_sessions
                        WHERE guild_id = ?
                        GROUP BY user_id
                    ),
                    users AS (
                        SELECT user_id FROM recorded
                        UNION
                        SELECT user_id FROM current_sessions
                    )
                    SELECT users.user_id,
                           COALESCE(recorded.seconds, 0)
                           + COALESCE(current_sessions.seconds, 0) AS seconds
                    FROM users
                    LEFT JOIN recorded USING (user_id)
                    LEFT JOIN current_sessions USING (user_id)
                    WHERE COALESCE(recorded.seconds, 0)
                          + COALESCE(current_sessions.seconds, 0) > 0
                    ORDER BY seconds DESC
                    """,
                    (
                        now, start_timestamp, guild_id, start_timestamp, now,
                        now, start_timestamp, guild_id,
                    ),
                )

            rows = await cursor.fetchall()
            return [
                (int(row["user_id"]), int(row["seconds"]))
                for row in rows
                if int(row["seconds"]) > 0
            ]


def is_eligible(member: discord.Member, state: discord.VoiceState) -> bool:
    if member.bot or state.channel is None:
        return False

    guild = member.guild
    if guild.afk_channel is not None and state.channel.id == guild.afk_channel.id:
        return False

    # Le temps n'est pas compté si le membre est muet ou sourd.
    if state.self_mute or state.mute or state.self_deaf or state.deaf:
        return False

    # Sur une scène, un membre dans le public n'est pas comptabilisé.
    if getattr(state, "suppress", False):
        return False

    return True


RANKING_PERIOD_LABELS = {
    "today": "Aujourd'hui",
    "week": "Cette semaine",
    "month": "Ce mois-ci",
    "year": "Cette année",
    "all": "Tous les temps",
}


def ranking_period_start(period: str) -> Optional[int]:
    """Calcule le début de période dans le fuseau horaire de la machine."""
    if period == "all":
        return None

    now = datetime.now().astimezone()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.fromtimestamp(
            start.timestamp() - start.weekday() * DAY,
            tz=start.tzinfo,
        )
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        start = now.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        raise ValueError(f"Période inconnue : {period}")

    return int(start.timestamp())


class VoiceMilestoneBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )
        self.db = VoiceDatabase(DATABASE_PATH)
        self.initialized_voice_sessions = False

    async def setup_hook(self) -> None:
        await self.db.connect()

        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(
                "Commandes synchronisées sur le serveur de test %s",
                TEST_GUILD_ID,
            )
        else:
            await self.tree.sync()
            log.info("Commandes globales synchronisées")

        checkpoint_sessions.start()

    async def close(self) -> None:
        checkpoint_sessions.cancel()
        await self.db.close()
        await super().close()


bot = VoiceMilestoneBot()


async def apply_milestone_roles(
    guild: discord.Guild,
    member: discord.Member,
    milestone_number: int,
) -> tuple[Optional[discord.Role], Optional[discord.Role], list[str]]:
    """Ajoute et retire les rôles configurés pour un palier."""
    role_configs = await bot.db.get_roles(guild.id, milestone_number)
    if not role_configs:
        return None, None, []

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None, None, ["Le bot n'a pas la permission « Gérer les rôles »."]

    added_role: Optional[discord.Role] = None
    removed_role: Optional[discord.Role] = None
    errors: list[str] = []

    for action in ("remove", "add"):
        role_id = role_configs.get(action)
        if role_id is None:
            continue

        role = guild.get_role(role_id)
        action_label = "à ajouter" if action == "add" else "à retirer"
        if role is None:
            errors.append(f"Le rôle {action_label} n'existe plus.")
            continue
        if role >= me.top_role:
            errors.append(f"Le rôle {role.mention} est au-dessus du rôle du bot.")
            continue

        try:
            if action == "remove":
                removed_role = role
                if role in member.roles:
                    await member.remove_roles(
                        role,
                        reason=f"Palier vocal {milestone_number} atteint",
                    )
            else:
                added_role = role
                if role not in member.roles:
                    await member.add_roles(
                        role,
                        reason=f"Palier vocal {milestone_number} atteint",
                    )
        except discord.Forbidden:
            operation = "l'attribution" if action == "add" else "le retrait"
            errors.append(f"Discord a refusé {operation} de {role.mention}.")
        except discord.HTTPException:
            log.exception(
                "Échec de l'action %s sur le rôle %s pour %s",
                action,
                role.id,
                member.id,
            )
            errors.append(f"Erreur Discord pendant la gestion de {role.mention}.")

    return added_role, removed_role, errors

def render_message(
    template: str,
    *,
    member: discord.Member,
    milestone: Milestone,
    total_seconds: int,
    role: Optional[discord.Role] = None,
) -> str:
    values = {
        "membre": member.mention,
        "palier": str(milestone.number),
        "duree": milestone.label,
        "temps": format_duration(total_seconds),
        "role": role.mention if role else "aucun rôle",
    }
    try:
        return template.format_map(values)
    except (KeyError, ValueError):
        log.warning("Modèle de message invalide pour le serveur %s", member.guild.id)
        return DEFAULT_MILESTONE_MESSAGE.format_map(values)


async def synchronize_member_milestones(
    guild: discord.Guild,
    member: discord.Member,
    total_seconds: int,
) -> tuple[int, int]:
    """Synchronise les rôles du membre avec son temps vocal."""
    milestones = await bot.db.get_milestones(guild.id)
    configured_roles = await bot.db.get_all_roles(guild.id)
    added_roles = 0
    removed_roles = 0

    assert bot.db.connection is not None
    async with bot.db.lock:
        for milestone in milestones:
            if total_seconds >= milestone.seconds:
                await bot.db.connection.execute(
                    """
                    INSERT OR IGNORE INTO announced_milestones
                        (guild_id, user_id, milestone, announced_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (guild.id, member.id, milestone.number, int(time.time())),
                )
        await bot.db.connection.commit()

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return added_roles, removed_roles

    for milestone in milestones:
        role_configs = configured_roles.get(milestone.number, {})
        reached = total_seconds >= milestone.seconds

        for action, role_id in role_configs.items():
            role = guild.get_role(role_id)
            if role is None or role >= me.top_role:
                continue

            has_role = role in member.roles
            try:
                if action == "add":
                    if reached and not has_role:
                        await member.add_roles(role, reason="Synchronisation du temps vocal")
                        added_roles += 1
                    elif not reached and has_role:
                        await member.remove_roles(role, reason="Synchronisation du temps vocal")
                        removed_roles += 1
                elif action == "remove" and reached and has_role:
                    await member.remove_roles(role, reason="Synchronisation du temps vocal")
                    removed_roles += 1
            except discord.Forbidden:
                log.warning("Impossible de synchroniser le rôle %s", role.id)
            except discord.HTTPException:
                log.exception("Erreur Discord pendant la synchronisation du rôle %s", role.id)

    return added_roles, removed_roles


async def announce_milestone(
    guild: discord.Guild,
    member: discord.Member,
    milestone: Milestone,
    total_seconds: int,
    added_role: Optional[discord.Role],
    removed_role: Optional[discord.Role],
    role_errors: list[str],
) -> None:
    channel_id = await bot.db.get_announcement_channel(guild.id)
    if channel_id is None:
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    milestone_template, role_template = await bot.db.get_messages(
        guild.id, milestone.number
    )
    description = render_message(
        milestone_template,
        member=member,
        milestone=milestone,
        total_seconds=total_seconds,
        role=added_role or removed_role,
    )

    if removed_role is not None:
        description += (
            "\n\n"
            f"Le rôle {removed_role.mention} a été retiré au palier "
            f"**{milestone.number}**."
        )

    if added_role is not None:
        description += "\n\n" + render_message(
            role_template,
            member=member,
            milestone=milestone,
            total_seconds=total_seconds,
            role=added_role,
        )

    embed = discord.Embed(
        title=f"🎉 Palier vocal {milestone.number} atteint !",
        description=description,
        colour=discord.Colour.gold(),
    )
    embed.add_field(name="Temps comptabilisé", value=format_duration(total_seconds))
    if role_errors:
        embed.add_field(
            name="Gestion des rôles",
            value="⚠️ " + "\n⚠️ ".join(role_errors),
            inline=False,
        )
    embed.set_thumbnail(url=member.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("Impossible d'écrire dans le salon %s", channel.id)
    except discord.HTTPException:
        log.exception("Erreur lors de l'annonce du palier")


async def process_milestones(
    guild: discord.Guild,
    member: discord.Member,
    old_total: int,
    new_total: int,
) -> None:
    milestones = await bot.db.get_milestones(guild.id)

    for milestone in milestones:
        if old_total < milestone.seconds <= new_total:
            first_time = await bot.db.mark_milestone_announced(
                guild.id, member.id, milestone.number
            )
            if not first_time:
                continue

            added_role, removed_role, role_errors = await apply_milestone_roles(
                guild, member, milestone.number
            )
            await announce_milestone(
                guild, member, milestone, new_total,
                added_role, removed_role, role_errors,
            )

            for role_error in role_errors:
                log.warning(
                    "Palier %s de %s atteint, mais action de rôle impossible : %s",
                    milestone.number, member.id, role_error,
                )


async def remove_milestone_roles(
    guild: discord.Guild,
    member: discord.Member,
) -> tuple[int, int]:
    """Retire uniquement les rôles configurés avec l'action add."""
    configured_roles = await bot.db.get_all_roles(guild.id)
    role_ids = {
        actions["add"]
        for actions in configured_roles.values()
        if "add" in actions
    }
    roles_to_remove = [
        role for role_id in role_ids
        if (role := guild.get_role(role_id)) is not None and role in member.roles
    ]

    if not roles_to_remove:
        return 0, 0

    me = guild.me
    removable_roles = [
        role for role in roles_to_remove
        if me is not None and me.guild_permissions.manage_roles and role < me.top_role
    ]
    errors = len(roles_to_remove) - len(removable_roles)

    if removable_roles:
        try:
            await member.remove_roles(
                *removable_roles, reason="Réinitialisation des statistiques vocales"
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Impossible de retirer les rôles de paliers de %s", member.id)
            return 0, len(roles_to_remove)

    return len(removable_roles), errors


async def restart_member_session_if_eligible(
    guild: discord.Guild,
    member: discord.Member,
    now: Optional[int] = None,
) -> None:
    """Redémarre le compteur à zéro si le membre est encore éligible en vocal."""
    state = member.voice
    if state is not None and is_eligible(member, state):
        await bot.db.begin_session(guild.id, member.id, now=now)


async def reset_guild_voice_stats(
    guild: discord.Guild,
) -> tuple[int, int, int]:
    """Réinitialise les statistiques et rôles de tous les membres du serveur."""
    known_user_ids = {
        user_id for user_id, _total in await bot.db.all_totals(guild.id)
    }

    for channel in guild.voice_channels + guild.stage_channels:
        known_user_ids.update(member.id for member in channel.members if not member.bot)

    members_reset = 0
    roles_removed = 0
    role_errors = 0

    for user_id in known_user_ids:
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue

        removed, errors = await remove_milestone_roles(guild, member)
        roles_removed += removed
        role_errors += errors
        members_reset += 1

    await bot.db.reset_guild_stats(guild.id)

    now = int(time.time())
    for channel in guild.voice_channels + guild.stage_channels:
        for member in channel.members:
            if not member.bot:
                await restart_member_session_if_eligible(guild, member, now=now)

    return members_reset, roles_removed, role_errors


class ResetAllConfirmationView(discord.ui.View):
    def __init__(self, author_id: int, guild: discord.Guild) -> None:
        super().__init__(timeout=60)
        self.author_id = author_id
        self.guild = guild
        self.message: Optional[discord.InteractionMessage] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Seule la personne ayant lancé la commande peut répondre.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(
                    content="⌛ La demande de réinitialisation a expiré.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Confirmer la réinitialisation",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        for item in self.children:
            item.disabled = True

        members_reset, roles_removed, role_errors = await reset_guild_voice_stats(
            self.guild
        )

        details = (
            "✅ Toutes les statistiques vocales du serveur ont été réinitialisées.\n"
            f"**{members_reset}** membre(s) traité(s), "
            f"**{roles_removed}** rôle(s) retiré(s)."
        )
        if role_errors:
            details += (
                f"\n⚠️ **{role_errors}** rôle(s) n'ont pas pu être retirés "
                "à cause des permissions ou de la hiérarchie des rôles."
            )

        await interaction.edit_original_response(content=details, view=self)
        self.stop()

    @discord.ui.button(
        label="Annuler",
        style=discord.ButtonStyle.secondary,
        emoji="❌",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content="❌ Réinitialisation annulée.",
            view=self,
        )
        self.stop()


@bot.event
async def on_ready() -> None:
    log.info(
        "Connecté en tant que %s (%s)",
        bot.user,
        bot.user.id if bot.user else "?",
    )

    # Après un redémarrage, la période pendant laquelle le bot était éteint
    # ne doit pas être ajoutée au temps vocal.
    if not bot.initialized_voice_sessions:
        await bot.db.clear_active_sessions()
        now = int(time.time())

        for guild in bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                for member in channel.members:
                    if member.voice and is_eligible(member, member.voice):
                        await bot.db.begin_session(guild.id, member.id, now)

        bot.initialized_voice_sessions = True


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return

    old_total, new_total = await bot.db.end_or_checkpoint_session(
        member.guild.id,
        member.id,
        keep_active=False,
    )

    if new_total > old_total:
        await process_milestones(member.guild, member, old_total, new_total)

    if is_eligible(member, after):
        await bot.db.begin_session(member.guild.id, member.id)


@tasks.loop(seconds=60)
async def checkpoint_sessions() -> None:
    now = int(time.time())

    for guild in bot.guilds:
        for channel in guild.voice_channels + guild.stage_channels:
            for member in channel.members:
                state = member.voice
                if state is None or not is_eligible(member, state):
                    continue

                old_total, new_total = await bot.db.end_or_checkpoint_session(
                    guild.id,
                    member.id,
                    keep_active=True,
                    now=now,
                )

                if new_total > old_total:
                    await process_milestones(
                        guild,
                        member,
                        old_total,
                        new_total,
                    )


@checkpoint_sessions.before_loop
async def before_checkpoint_sessions() -> None:
    await bot.wait_until_ready()


config_group = app_commands.Group(
    name="config",
    description="Configuration administrative du suivi vocal",
    default_permissions=discord.Permissions(manage_guild=True),
)


@config_group.command(
    name="annonce",
    description="Choisir le salon des annonces de paliers",
)
@app_commands.describe(
    salon="Salon textuel dans lequel publier les annonces",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def config_channel(
    interaction: discord.Interaction,
    salon: discord.TextChannel,
) -> None:
    assert interaction.guild is not None
    await bot.db.set_announcement_channel(interaction.guild.id, salon.id)
    await interaction.response.send_message(
        f"✅ Les annonces de paliers seront publiées dans {salon.mention}.",
        ephemeral=True,
    )


@config_group.command(
    name="palier-modifier",
    description="Modifier la durée nécessaire pour atteindre un palier",
)
@app_commands.describe(
    palier="Numéro du palier à modifier, entre 1 et 20",
    jours="Nombre de jours",
    heures="Nombre d'heures supplémentaires",
    minutes="Nombre de minutes supplémentaires",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def modify_milestone(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    jours: app_commands.Range[int, 0, 3650] = 0,
    heures: app_commands.Range[int, 0, 23] = 0,
    minutes: app_commands.Range[int, 0, 59] = 0,
) -> None:
    assert interaction.guild is not None

    total_seconds = int(jours) * DAY + int(heures) * HOUR + int(minutes) * 60
    if total_seconds <= 0:
        await interaction.response.send_message(
            "❌ La durée doit être supérieure à zéro.",
            ephemeral=True,
        )
        return

    milestones = await bot.db.get_milestones(interaction.guild.id)
    milestone_number = int(palier)

    previous_milestone = next(
        (m for m in milestones if m.number == milestone_number - 1),
        None,
    )
    next_milestone = next(
        (m for m in milestones if m.number == milestone_number + 1),
        None,
    )

    if previous_milestone and total_seconds <= previous_milestone.seconds:
        await interaction.response.send_message(
            "❌ Ce palier doit être supérieur au palier précédent "
            f"(**{previous_milestone.label}**).",
            ephemeral=True,
        )
        return

    if next_milestone and total_seconds >= next_milestone.seconds:
        await interaction.response.send_message(
            "❌ Ce palier doit être inférieur au palier suivant "
            f"(**{next_milestone.label}**).",
            ephemeral=True,
        )
        return

    await bot.db.set_milestone_time(
        interaction.guild.id,
        milestone_number,
        total_seconds,
    )

    await interaction.response.send_message(
        f"✅ Le palier **{milestone_number}** est maintenant fixé à "
        f"**{format_milestone_duration(total_seconds)}**.",
        ephemeral=True,
    )


@config_group.command(
    name="palier-reinit",
    description="Rétablir la durée par défaut d'un palier",
)
@app_commands.describe(
    palier="Numéro du palier à réinitialiser, entre 1 et 20",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_milestone(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
) -> None:
    assert interaction.guild is not None

    milestone_number = int(palier)
    default = DEFAULT_MILESTONE_BY_NUMBER[milestone_number]
    milestones = await bot.db.get_milestones(interaction.guild.id)

    previous_milestone = next(
        (m for m in milestones if m.number == milestone_number - 1),
        None,
    )
    next_milestone = next(
        (m for m in milestones if m.number == milestone_number + 1),
        None,
    )

    if previous_milestone and default.seconds <= previous_milestone.seconds:
        await interaction.response.send_message(
            "❌ Impossible de réinitialiser ce palier : sa durée par défaut "
            "ne serait pas supérieure au palier précédent personnalisé.",
            ephemeral=True,
        )
        return

    if next_milestone and default.seconds >= next_milestone.seconds:
        await interaction.response.send_message(
            "❌ Impossible de réinitialiser ce palier : sa durée par défaut "
            "ne serait pas inférieure au palier suivant personnalisé.",
            ephemeral=True,
        )
        return

    await bot.db.reset_milestone_time(interaction.guild.id, milestone_number)
    await interaction.response.send_message(
        f"✅ Le palier **{milestone_number}** a été réinitialisé à "
        f"**{default.label}**.",
        ephemeral=True,
    )


@config_group.command(
    name="paliers-reinit",
    description="Rétablir les durées par défaut de tous les paliers",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_all_milestones(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await bot.db.reset_all_milestone_times(interaction.guild.id)
    await interaction.response.send_message(
        "✅ Toutes les durées des paliers ont été réinitialisées.",
        ephemeral=True,
    )


@config_group.command(
    name="palier-message",
    description="Personnaliser le message d'annonce d'un palier",
)
@app_commands.describe(
    palier="Numéro du palier, entre 1 et 20",
    message="Message avec variables : {membre}, {palier}, {duree}, {temps}, {role}",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def config_milestone_message(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    message: app_commands.Range[str, 1, 1800],
) -> None:
    assert interaction.guild is not None
    await bot.db.set_milestone_message(
        interaction.guild.id, int(palier), str(message)
    )
    await interaction.response.send_message(
        f"✅ Message du palier **{palier}** enregistré.\n"
        f"Variables disponibles : `{MESSAGE_PLACEHOLDERS}`",
        ephemeral=True,
    )


@config_group.command(
    name="role-message",
    description="Personnaliser le message envoyé lors de l'attribution d'un rôle",
)
@app_commands.describe(
    palier="Numéro du palier, entre 1 et 20",
    message="Message avec variables : {membre}, {palier}, {duree}, {temps}, {role}",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def config_role_message(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    message: app_commands.Range[str, 1, 1800],
) -> None:
    assert interaction.guild is not None
    await bot.db.set_role_message(
        interaction.guild.id, int(palier), str(message)
    )
    await interaction.response.send_message(
        f"✅ Message d'attribution du rôle pour le palier **{palier}** enregistré.\n"
        f"Variables disponibles : `{MESSAGE_PLACEHOLDERS}`",
        ephemeral=True,
    )


@config_group.command(
    name="message-reinit",
    description="Rétablir un message par défaut",
)
@app_commands.describe(
    palier="Numéro du palier, entre 1 et 20",
    type_message="Message de palier ou message d'attribution du rôle",
)
@app_commands.choices(
    type_message=[
        app_commands.Choice(name="Message de palier", value="palier"),
        app_commands.Choice(name="Message de rôle", value="role"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_config_message(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    type_message: app_commands.Choice[str],
) -> None:
    assert interaction.guild is not None
    await bot.db.reset_milestone_message(
        interaction.guild.id, int(palier), type_message.value
    )
    await interaction.response.send_message(
        f"✅ Le {type_message.name.lower()} du palier **{palier}** a été réinitialisé.",
        ephemeral=True,
    )


@config_group.command(
    name="role-associer",
    description="Ajouter et/ou retirer un rôle lorsqu'un palier est atteint",
)
@app_commands.describe(
    palier="Numéro du palier, entre 1 et 20",
    role_ajouter="Rôle à attribuer lorsque le palier est atteint",
    role_retirer="Rôle à retirer lorsque le palier est atteint",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def config_role(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    role_ajouter: Optional[discord.Role] = None,
    role_retirer: Optional[discord.Role] = None,
) -> None:
    assert interaction.guild is not None
    guild = interaction.guild
    me = guild.me

    if role_ajouter is None and role_retirer is None:
        await interaction.response.send_message(
            "❌ Indique au moins un rôle à ajouter ou à retirer.",
            ephemeral=True,
        )
        return

    if role_ajouter is not None and role_retirer is not None:
        if role_ajouter.id == role_retirer.id:
            await interaction.response.send_message(
                "❌ Le même rôle ne peut pas être ajouté et retiré au même palier.",
                ephemeral=True,
            )
            return

    for role in (role_ajouter, role_retirer):
        if role is None:
            continue
        if role.is_default():
            await interaction.response.send_message(
                "❌ Le rôle `@everyone` ne peut pas être utilisé.",
                ephemeral=True,
            )
            return
        if me is None or role >= me.top_role:
            await interaction.response.send_message(
                f"❌ Place le rôle du bot au-dessus de {role.mention} dans Discord.",
                ephemeral=True,
            )
            return

    milestone_number = int(palier)
    if role_ajouter is not None:
        await bot.db.set_role(
            guild.id, milestone_number, "add", role_ajouter.id
        )
    if role_retirer is not None:
        await bot.db.set_role(
            guild.id, milestone_number, "remove", role_retirer.id
        )

    milestones = await bot.db.get_milestones(guild.id)
    milestone = next(m for m in milestones if m.number == milestone_number)
    actions: list[str] = []
    if role_retirer is not None:
        actions.append(f"retirer {role_retirer.mention}")
    if role_ajouter is not None:
        actions.append(f"ajouter {role_ajouter.mention}")

    await interaction.response.send_message(
        f"✅ Au palier **{milestone_number}** (**{milestone.label}**), "
        + " et ".join(actions)
        + ".",
        ephemeral=True,
    )


@config_group.command(
    name="role-retirer",
    description="Supprimer une ou toutes les actions de rôle d'un palier",
)
@app_commands.describe(
    palier="Numéro du palier, entre 1 et 20",
    action="Action à supprimer",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="Rôle à ajouter", value="add"),
        app_commands.Choice(name="Rôle à retirer", value="remove"),
        app_commands.Choice(name="Toutes les actions", value="all"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_config_role(
    interaction: discord.Interaction,
    palier: app_commands.Range[int, 1, 20],
    action: app_commands.Choice[str],
) -> None:
    assert interaction.guild is not None
    selected_action = None if action.value == "all" else action.value
    await bot.db.remove_role(interaction.guild.id, int(palier), selected_action)
    await interaction.response.send_message(
        f"✅ Configuration « {action.name} » supprimée pour le palier **{palier}**.",
        ephemeral=True,
    )


@config_group.command(
    name="afficher",
    description="Afficher la configuration actuelle",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def show_config(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    guild = interaction.guild

    channel_id = await bot.db.get_announcement_channel(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    roles = await bot.db.get_all_roles(guild.id)
    milestones = await bot.db.get_milestones(guild.id)

    embed = discord.Embed(
        title="Configuration du suivi vocal",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="Salon d'annonces",
        value=(
            channel.mention
            if isinstance(channel, discord.TextChannel)
            else "Non configuré"
        ),
        inline=False,
    )

    milestone_lines = [
        f"**{m.number}.** {m.label}"
        for m in milestones
    ]
    embed.add_field(
        name="Durées des paliers",
        value="\n".join(milestone_lines),
        inline=False,
    )

    customized_messages: list[str] = []
    assert bot.db.connection is not None
    async with bot.db.lock:
        cursor = await bot.db.connection.execute(
            """
            SELECT milestone, milestone_message, role_message
            FROM milestone_messages
            WHERE guild_id = ?
            ORDER BY milestone
            """,
            (guild.id,),
        )
        message_rows = await cursor.fetchall()
    for row in message_rows:
        kinds: list[str] = []
        if row["milestone_message"]:
            kinds.append("palier")
        if row["role_message"]:
            kinds.append("rôle")
        if kinds:
            customized_messages.append(
                f"**{int(row['milestone'])}.** {', '.join(kinds)}"
            )
    embed.add_field(
        name="Messages personnalisés",
        value=(
            "\n".join(customized_messages)
            if customized_messages
            else "Aucun message personnalisé."
        ),
        inline=False,
    )

    if not roles:
        role_text = "Aucun rôle configuré."
    else:
        milestone_by_number = {m.number: m for m in milestones}
        role_lines: list[str] = []
        for milestone_number, actions in roles.items():
            milestone = milestone_by_number[milestone_number]
            details: list[str] = []
            remove_id = actions.get("remove")
            add_id = actions.get("add")
            if remove_id is not None:
                role = guild.get_role(remove_id)
                details.append(
                    f"retirer {role.mention if role else '`rôle supprimé`'}"
                )
            if add_id is not None:
                role = guild.get_role(add_id)
                details.append(
                    f"ajouter {role.mention if role else '`rôle supprimé`'}"
                )
            role_lines.append(
                f"**{milestone_number}.** {milestone.label} → "
                + " puis ".join(details)
            )
        role_text = "\n".join(role_lines)

    embed.add_field(name="Rôles", value=role_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@config_group.command(
    name="roles-synchroniser",
    description="Synchroniser les rôles avec les paliers déjà atteints",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def sync_roles(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    actions_done = 0
    errors = 0

    for user_id, _stored_total in await bot.db.all_totals(guild.id):
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue

        before_roles = {role.id for role in member.roles}
        total = await bot.db.get_total(guild.id, user_id)
        await synchronize_member_milestones(guild, member, total)
        after_roles = {role.id for role in member.roles}
        actions_done += len(before_roles.symmetric_difference(after_roles))

    await interaction.followup.send(
        f"✅ Synchronisation terminée : **{actions_done}** action(s) de rôle, "
        f"**{errors}** erreur(s).",
        ephemeral=True,
    )


reset_group = app_commands.Group(
    name="reset",
    description="Réinitialiser les statistiques vocales",
    default_permissions=discord.Permissions(administrator=True),
)


@reset_group.command(
    name="membre",
    description="Réinitialiser les statistiques vocales d'un membre",
)
@app_commands.describe(
    membre="Membre dont les statistiques vocales doivent être réinitialisées",
)
@app_commands.checks.has_permissions(administrator=True)
async def reset_member_voice_stats(
    interaction: discord.Interaction,
    membre: discord.Member,
) -> None:
    assert interaction.guild is not None
    guild = interaction.guild

    await interaction.response.defer(ephemeral=True, thinking=True)

    roles_removed, role_errors = await remove_milestone_roles(guild, membre)
    await bot.db.reset_member_stats(guild.id, membre.id)
    await restart_member_session_if_eligible(guild, membre)

    message = (
        f"✅ Les statistiques vocales de {membre.mention} ont été réinitialisées.\n"
        f"**{roles_removed}** rôle(s) de palier retiré(s)."
    )
    if role_errors:
        message += (
            f"\n⚠️ **{role_errors}** rôle(s) n'ont pas pu être retirés "
            "à cause des permissions ou de la hiérarchie des rôles."
        )

    await interaction.followup.send(message, ephemeral=True)


@reset_group.command(
    name="tous",
    description="Réinitialiser les statistiques vocales de tous les membres",
)
@app_commands.checks.has_permissions(administrator=True)
async def reset_all_voice_stats(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None

    view = ResetAllConfirmationView(interaction.user.id, interaction.guild)
    await interaction.response.send_message(
        "⚠️ Cette action supprimera définitivement toutes les statistiques "
        "vocales du serveur et retirera les rôles associés aux paliers.\n"
        "**Confirmer la réinitialisation ?**",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


config_group.add_command(reset_group)

@config_group.command(
    name="temps-modifier",
    description="Modifier manuellement le temps vocal total d'un membre",
)
@app_commands.describe(
    membre="Membre dont le temps vocal doit être modifié",
    jours="Nombre de jours à enregistrer",
    heures="Nombre d'heures supplémentaires",
    minutes="Nombre de minutes supplémentaires",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def modify_member_voice_time(
    interaction: discord.Interaction,
    membre: discord.Member,
    jours: app_commands.Range[int, 0, 36500] = 0,
    heures: app_commands.Range[int, 0, 23] = 0,
    minutes: app_commands.Range[int, 0, 59] = 0,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Cette commande doit être utilisée dans un serveur.",
            ephemeral=True,
        )
        return

    if membre.bot:
        await interaction.response.send_message(
            "❌ Le temps vocal d'un bot ne peut pas être modifié.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild

    new_total = (
        int(jours) * DAY
        + int(heures) * HOUR
        + int(minutes) * 60
    )

    old_total = await bot.db.get_total(
        guild.id,
        membre.id,
        include_active=True,
    )

    await bot.db.set_member_total(
        guild.id,
        membre.id,
        new_total,
    )

    added_roles, removed_roles = await synchronize_member_milestones(
        guild,
        membre,
        new_total,
    )

    embed = discord.Embed(
        title="Temps vocal modifié",
        colour=discord.Colour.green(),
    )

    embed.set_thumbnail(url=membre.display_avatar.url)

    embed.add_field(
        name="Membre",
        value=membre.mention,
        inline=False,
    )

    embed.add_field(
        name="Ancien temps",
        value=format_duration(old_total),
        inline=True,
    )

    embed.add_field(
        name="Nouveau temps",
        value=format_duration(new_total),
        inline=True,
    )

    embed.add_field(
        name="Rôles synchronisés",
        value=(
            f"Ajoutés : **{added_roles}**\n"
            f"Retirés : **{removed_roles}**"
        ),
        inline=False,
    )

    embed.set_footer(
        text=f"Modification effectuée par {interaction.user}"
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


bot.tree.add_command(config_group)


@bot.tree.command(
    name="membre-afficher",
    description="Afficher le temps vocal actif d'un membre",
)
@app_commands.describe(
    membre="Membre à consulter ; vous-même si omis",
)
async def vocal_time(
    interaction: discord.Interaction,
    membre: Optional[discord.Member] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Cette commande doit être utilisée dans un serveur.",
            ephemeral=True,
        )
        return

    target = membre or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message(
            "Membre introuvable.",
            ephemeral=True,
        )
        return

    total = await bot.db.get_total(interaction.guild.id, target.id)
    milestones = await bot.db.get_milestones(interaction.guild.id)

    reached = [m for m in milestones if total >= m.seconds]
    next_milestone = next(
        (m for m in milestones if total < m.seconds),
        None,
    )

    embed = discord.Embed(
        title=f"Temps vocal de {target.display_name}",
        colour=discord.Colour.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="Temps actif",
        value=f"**{format_duration(total)}**",
        inline=False,
    )
    embed.add_field(
        name="Dernier palier",
        value=(
            f"Palier {reached[-1].number} — {reached[-1].label}"
            if reached
            else "Aucun palier atteint"
        ),
        inline=False,
    )

    if next_milestone:
        remaining = max(0, next_milestone.seconds - total)
        embed.add_field(
            name="Prochain palier",
            value=(
                f"Palier {next_milestone.number} — {next_milestone.label}\n"
                f"Encore **{format_duration(remaining)}**"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Prochain palier",
            value="Tous les paliers sont atteints.",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="classement",
    description="Afficher le classement du temps vocal des membres",
)
@app_commands.describe(
    periode="Période utilisée pour calculer le classement",
)
@app_commands.choices(
    periode=[
        app_commands.Choice(name="Aujourd'hui", value="today"),
        app_commands.Choice(name="Cette semaine", value="week"),
        app_commands.Choice(name="Ce mois-ci", value="month"),
        app_commands.Choice(name="Cette année", value="year"),
        app_commands.Choice(name="Tous les temps", value="all"),
    ]
)
async def voice_leaderboard(
    interaction: discord.Interaction,
    periode: app_commands.Choice[str],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Cette commande doit être utilisée dans un serveur.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    period_value = periode.value
    start_timestamp = ranking_period_start(period_value)
    ranking = await bot.db.get_leaderboard(
        interaction.guild.id,
        start_timestamp=start_timestamp,
    )

    # Ignore les comptes ayant quitté le serveur et les bots.
    visible_ranking: list[tuple[discord.Member, int]] = []
    for user_id, seconds in ranking:
        member = interaction.guild.get_member(user_id)
        if member is not None and not member.bot:
            visible_ranking.append((member, seconds))

    period_label = RANKING_PERIOD_LABELS[period_value]
    if not visible_ranking:
        await interaction.followup.send(
            f"Aucune activité vocale enregistrée pour **{period_label.lower()}**."
        )
        return

    page_size = 25
    pages = [
        visible_ranking[index:index + page_size]
        for index in range(0, len(visible_ranking), page_size)
    ]

    for page_number, page in enumerate(pages, start=1):
        first_position = (page_number - 1) * page_size + 1
        lines: list[str] = []

        for offset, (member, seconds) in enumerate(page):
            position = first_position + offset
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"**{position}.**")
            lines.append(
                f"{medal} {member.mention} — **{format_duration(seconds)}**"
            )

        embed = discord.Embed(
            title=f"Classement vocal — {period_label}",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=(
                f"Page {page_number}/{len(pages)} • "
                f"{len(visible_ranking)} membre(s) classé(s)"
            )
        )

        await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="paliers-afficher",
    description="Afficher la liste des paliers vocaux",
)
async def list_milestones(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Cette commande doit être utilisée dans un serveur.",
            ephemeral=True,
        )
        return

    milestones = await bot.db.get_milestones(interaction.guild.id)
    lines = [f"**{m.number}.** {m.label}" for m in milestones]

    embed = discord.Embed(
        title="Paliers de temps vocal actif",
        description="\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ Tu dois avoir la permission « Gérer le serveur »."
    else:
        # Les erreurs des commandes sont souvent enveloppées
        # dans CommandInvokeError.
        original_error = getattr(error, "original", error)

        log.error(
            "Erreur pendant la commande /%s : %s",
            interaction.command.qualified_name if interaction.command else "?",
            original_error,
            exc_info=(
                type(original_error),
                original_error,
                original_error.__traceback__,
            ),
        )

        message = (
            "❌ Une erreur est survenue pendant l'exécution de la commande.\n"
            f"```{type(original_error).__name__}: {original_error}```"
        )

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError(
        "La variable DISCORD_TOKEN est absente. "
        "Copie .env.example vers .env et ajoute le token du bot."
    )

bot.run(TOKEN)
