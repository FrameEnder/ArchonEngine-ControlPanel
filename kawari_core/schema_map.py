"""Schema map for Kawari's SQLite databases.

AUTO-GENERATED from the upstream Diesel migrations at redstrate/Kawari @ 49c945011 (patch 7.56h1)
(servers/world/migrations/current_world/up.sql and the login equivalent).

``default`` is the literal used when synthesising an ``ALTER TABLE ... ADD COLUMN``
for a database that predates the column. TEXT columns holding bitmasks are stored
as JSON arrays of a fixed length -- the lengths come from resources/data/constants.yml
and MUST be exact, because Bitmask::from_sql does
``serde_json::from_str(..).ok().unwrap()`` and will panic the world server on a
malformed or wrongly-sized value.

A default of ``None`` means the column carries structured per-character data that
cannot be invented (inventory contents, chara_make, the character's name). The
migration engine refuses to add those and tells the user to recreate instead.
"""

SCHEMA = {
    "world": {
        "unlock": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "unlocks": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "seen_active_help": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "minions": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "mounts": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "orchestrion_rolls": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "cutscene_seen": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "ornaments": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0]'"
                },
                "caught_fish": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "caught_spearfish": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "adventures": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "triple_triad_cards": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "glasses_styles": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0]'"
                },
                "chocobo_taxi_stands": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "titles": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `unlock`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`unlocks` TEXT NOT NULL,\n\t`seen_active_help` TEXT NOT NULL,\n\t`minions` TEXT NOT NULL,\n\t`mounts` TEXT NOT NULL,\n\t`orchestrion_rolls` TEXT NOT NULL,\n\t`cutscene_seen` TEXT NOT NULL,\n\t`ornaments` TEXT NOT NULL,\n\t`caught_fish` TEXT NOT NULL,\n\t`caught_spearfish` TEXT NOT NULL,\n\t`adventures` TEXT NOT NULL,\n\t`triple_triad_cards` TEXT NOT NULL,\n\t`glasses_styles` TEXT NOT NULL,\n\t`chocobo_taxi_stands` TEXT NOT NULL,\n\t`titles` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "mentor": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "version": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "is_battle": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "is_trade": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "is_novice": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "is_returner": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `mentor`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`version` INTEGER NOT NULL,\n\t`is_battle` INTEGER NOT NULL,\n\t`is_trade` INTEGER NOT NULL,\n\t`is_novice` INTEGER NOT NULL,\n\t`is_returner` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "customize": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "chara_make": {
                    "type": "TEXT",
                    "default": None
                },
                "city_state": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "remake_mode": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `customize`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`chara_make` TEXT NOT NULL,\n\t`city_state` INTEGER NOT NULL,\n\t`remake_mode` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "search_info": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "online_status": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "comment": {
                    "type": "TEXT",
                    "default": "''"
                },
                "selected_languages": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `search_info`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`online_status` INTEGER NOT NULL,\n\t`comment` TEXT NOT NULL,\n\t`selected_languages` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "friends": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "friend_content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "group_icon": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "invite_time": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "is_pending": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `friends`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`content_id` BIGINT NOT NULL,\n\t`friend_content_id` BIGINT NOT NULL,\n\t`group_icon` INTEGER NOT NULL,\n\t`invite_time` BIGINT NOT NULL,\n\t`is_pending` INTEGER NOT NULL\n);"
        },
        "quest": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "completed": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "active": {
                    "type": "TEXT",
                    "default": "'[]'"
                },
                "completed_legacy": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "unlocked_map_markers": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "completed_levequests": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "gathered_gathering_items": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "shared_fates": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `quest`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`completed` TEXT NOT NULL,\n\t`active` TEXT NOT NULL,\n\t`completed_legacy` TEXT NOT NULL,\n\t`unlocked_map_markers` TEXT NOT NULL,\n\t`completed_levequests` TEXT NOT NULL,\n\t`gathered_gathering_items` TEXT NOT NULL,\n\t`shared_fates` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "classjob": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "current_class": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "levels": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "exp": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "first_class": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "rested_exp": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `classjob`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`current_class` INTEGER NOT NULL,\n\t`levels` TEXT NOT NULL,\n\t`exp` TEXT NOT NULL,\n\t`first_class` INTEGER NOT NULL,\n\t`rested_exp` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "linkshell_members": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "linkshell_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "invite_time": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "rank": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `linkshell_members`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`content_id` BIGINT NOT NULL,\n\t`linkshell_id` BIGINT NOT NULL,\n\t`invite_time` BIGINT NOT NULL,\n\t`rank` INTEGER NOT NULL\n);"
        },
        "grand_company": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "active_company": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "company_ranks": {
                    "type": "TEXT",
                    "default": "'[0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `grand_company`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`active_company` INTEGER NOT NULL,\n\t`company_ranks` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "linkshells": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "name": {
                    "type": "TEXT",
                    "default": "''"
                },
                "creation_time": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "is_crossworld": {
                    "type": "BOOL",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `linkshells`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`name` TEXT NOT NULL,\n\t`creation_time` BIGINT NOT NULL,\n\t`is_crossworld` BOOL NOT NULL\n);"
        },
        "buddy": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "name": {
                    "type": "TEXT",
                    "default": "''"
                },
                "rank": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "stars": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "levels": {
                    "type": "TEXT",
                    "default": "'[0,0,0]'"
                },
                "exp": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "color": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `buddy`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`name` TEXT NOT NULL,\n\t`rank` INTEGER NOT NULL,\n\t`stars` INTEGER NOT NULL,\n\t`levels` TEXT NOT NULL,\n\t`exp` INTEGER NOT NULL,\n\t`color` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "party": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "leader_content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "members": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `party`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`leader_content_id` BIGINT NOT NULL,\n\t`members` TEXT NOT NULL\n);"
        },
        "mail": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "kind": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "read": {
                    "type": "BOOL",
                    "default": "0"
                },
                "timestamp": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "recipient_content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "sender_content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "message": {
                    "type": "TEXT",
                    "default": "''"
                },
                "attached_items": {
                    "type": "TEXT",
                    "default": "''"
                }
            },
            "ddl": "CREATE TABLE `mail`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`kind` INTEGER NOT NULL,\n\t`read` BOOL NOT NULL,\n\t`timestamp` BIGINT NOT NULL,\n\t`recipient_content_id` BIGINT NOT NULL,\n\t`sender_content_id` BIGINT NOT NULL,\n\t`message` TEXT NOT NULL,\n\t`attached_items` TEXT NOT NULL\n);"
        },
        "inventory": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "contents": {
                    "type": "TEXT",
                    "default": None
                },
                "equipped_glasses_ids": {
                    "type": "TEXT",
                    "default": "'[0,0]'"
                }
            },
            "ddl": "CREATE TABLE `inventory`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`contents` TEXT NOT NULL,\n\t`equipped_glasses_ids` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "volatile": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "position": {
                    "type": "TEXT",
                    "default": "'{\"x\":0.0,\"y\":0.0,\"z\":0.0}'"
                },
                "rotation": {
                    "type": "DOUBLE",
                    "default": "0"
                },
                "zone_id": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "display_flags": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "title": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "is_online": {
                    "type": "BOOL",
                    "default": "0"
                },
                "client_language": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "mode": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "mode_arg": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "current_mount": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `volatile`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`position` TEXT NOT NULL,\n\t`rotation` DOUBLE NOT NULL,\n\t`zone_id` INTEGER NOT NULL,\n\t`display_flags` INTEGER NOT NULL,\n\t`title` INTEGER NOT NULL,\n\t`is_online` BOOL NOT NULL,\n\t`client_language` INTEGER NOT NULL,\n\t`mode` INTEGER NOT NULL,\n\t`mode_arg` INTEGER NOT NULL,\n\t`current_mount` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "aetheryte": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "unlocked": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "homepoint": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "favorite_aetherytes": {
                    "type": "TEXT",
                    "default": "'[]'"
                },
                "free_aetheryte": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `aetheryte`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`unlocked` TEXT NOT NULL,\n\t`homepoint` INTEGER NOT NULL,\n\t`favorite_aetherytes` TEXT NOT NULL,\n\t`free_aetheryte` INTEGER NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "companion": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "unlocked_equip": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `companion`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`unlocked_equip` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "content": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "unlocked_special_content": {
                    "type": "TEXT",
                    "default": "'[0]'"
                },
                "unlocked_raids": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "unlocked_dungeons": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "unlocked_guildhests": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0]'"
                },
                "unlocked_trials": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "unlocked_crystalline_conflicts": {
                    "type": "TEXT",
                    "default": "'[0,0,0]'"
                },
                "unlocked_frontlines": {
                    "type": "TEXT",
                    "default": "'[0,0]'"
                },
                "cleared_raids": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "cleared_dungeons": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "cleared_guildhests": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0]'"
                },
                "cleared_trials": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                },
                "cleared_crystalline_conflicts": {
                    "type": "TEXT",
                    "default": "'[0,0,0]'"
                },
                "cleared_frontlines": {
                    "type": "TEXT",
                    "default": "'[0,0]'"
                },
                "cleared_masked_carnivale": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0]'"
                },
                "unlocked_misc_content": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0]'"
                },
                "cleared_misc_content": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `content`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`unlocked_special_content` TEXT NOT NULL,\n\t`unlocked_raids` TEXT NOT NULL,\n\t`unlocked_dungeons` TEXT NOT NULL,\n\t`unlocked_guildhests` TEXT NOT NULL,\n\t`unlocked_trials` TEXT NOT NULL,\n\t`unlocked_crystalline_conflicts` TEXT NOT NULL,\n\t`unlocked_frontlines` TEXT NOT NULL,\n\t`cleared_raids` TEXT NOT NULL,\n\t`cleared_dungeons` TEXT NOT NULL,\n\t`cleared_guildhests` TEXT NOT NULL,\n\t`cleared_trials` TEXT NOT NULL,\n\t`cleared_crystalline_conflicts` TEXT NOT NULL,\n\t`cleared_frontlines` TEXT NOT NULL,\n\t`cleared_masked_carnivale` TEXT NOT NULL,\n\t`unlocked_misc_content` TEXT NOT NULL,\n\t`cleared_misc_content` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        },
        "character": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "service_account_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "actor_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "gm_rank": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "name": {
                    "type": "TEXT",
                    "default": None
                },
                "time_played_minutes": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "warrior_of_light": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `character`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`service_account_id` BIGINT NOT NULL,\n\t`actor_id` BIGINT NOT NULL,\n\t`gm_rank` INTEGER NOT NULL,\n\t`name` TEXT NOT NULL,\n\t`time_played_minutes` BIGINT NOT NULL,\n\t`warrior_of_light` INTEGER NOT NULL\n);"
        },
        "aether_current": {
            "columns": {
                "content_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "comp_flg_set": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0]'"
                },
                "unlocked": {
                    "type": "TEXT",
                    "default": "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]'"
                }
            },
            "ddl": "CREATE TABLE `aether_current`(\n\t`content_id` BIGINT NOT NULL PRIMARY KEY,\n\t`comp_flg_set` TEXT NOT NULL,\n\t`unlocked` TEXT NOT NULL,\n\tFOREIGN KEY (`content_id`) REFERENCES `character`(`content_id`)\n);"
        }
    },
    "login": {
        "service_account": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "user_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "max_ex": {
                    "type": "INTEGER",
                    "default": "0"
                },
                "legacy": {
                    "type": "INTEGER",
                    "default": "0"
                }
            },
            "ddl": "CREATE TABLE `service_account`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`user_id` BIGINT NOT NULL,\n\t`max_ex` INTEGER NOT NULL,\n\t`legacy` INTEGER NOT NULL,\n\tFOREIGN KEY (`user_id`) REFERENCES `user`(`id`)\n);"
        },
        "user": {
            "columns": {
                "id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "username": {
                    "type": "TEXT",
                    "default": None
                },
                "password": {
                    "type": "TEXT",
                    "default": None
                }
            },
            "ddl": "CREATE TABLE `user`(\n\t`id` BIGINT NOT NULL PRIMARY KEY,\n\t`username` TEXT NOT NULL,\n\t`password` TEXT NOT NULL\n);"
        },
        "session": {
            "columns": {
                "user_id": {
                    "type": "BIGINT",
                    "default": "0"
                },
                "time": {
                    "type": "TEXT",
                    "default": None
                },
                "service": {
                    "type": "TEXT",
                    "default": None
                },
                "sid": {
                    "type": "TEXT",
                    "default": None
                }
            },
            "ddl": "CREATE TABLE `session`(\n\t`user_id` BIGINT NOT NULL,\n\t`time` TEXT NOT NULL,\n\t`service` TEXT NOT NULL,\n\t`sid` TEXT NOT NULL,\n\tPRIMARY KEY(`user_id`, `service`),\n\tFOREIGN KEY (`user_id`) REFERENCES `user`(`id`)\n);"
        }
    }
}
