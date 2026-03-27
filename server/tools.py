"""
Backward-compatibility re-export. New code should import from domain modules directly.

Canonical module locations:
  roll_utils        — VaultCache, roll helpers, template builders
  combat_tools      — attack, damage, conditions, combat actions
  entity_tools      — create/spawn/update entities
  item_tools        — equipment, inventory, items
  map_tools         — map geometry, terrain, traps, lights
  spatial_tools     — movement, positioning
  combat_flow_tools — initiative, combat state
  narrative_tools   — storylets, graph mutations, backstory
  knowledge_tools   — bestiary, rulebook, campaign queries
  world_tools       — time, rest, dice, encounter generation
  vault_io          — journal editing, KG/storylet sync
"""

# -------------------------------------------------------------------
# vault_io helpers
# -------------------------------------------------------------------
from vault_io import (
    upsert_journal_section,
    write_audit_log,
    get_journals_dir,
    read_markdown_entity,
    edit_markdown_entity,
)

# -------------------------------------------------------------------
# roll_utils
# -------------------------------------------------------------------
from roll_utils import (
    VaultCache,
    _VAULT_CACHE,
    update_roll_automations,
    get_roll_automations,
    _calculate_reach,
    _build_npc_template,
    _build_location_template,
    _build_faction_template,
    _build_pc_template,
    _build_party_tracker,
    _get_config_tone,
    _get_config_settings,
    _get_config_dirs,
    _search_markdown_for_keywords,
    _get_entity_by_name,
    _get_current_combat_initiative,
)

# -------------------------------------------------------------------
# combat_tools
# -------------------------------------------------------------------
from combat_tools import (
    execute_melee_attack,
    modify_health,
    use_ability_or_spell,
    toggle_condition,
    execute_grapple_or_shove,
    use_dash_action,
    ready_action,
    clear_readied_action,
    drop_concentration,
    manage_mount,
    trigger_environmental_hazard,
    interact_with_object,
    evaluate_extreme_weather,
    hide_entity,
    detect_hidden,
    command_companion,
)

# -------------------------------------------------------------------
# entity_tools
# -------------------------------------------------------------------
from entity_tools import (
    create_new_entity,
    flesh_out_entity,
    update_yaml_frontmatter,
    fetch_entity_context,
    level_up_character,
    update_character_status,
    spawn_summon,
    propose_entity_creation,
    generate_side_quests_for_entity,
    mark_entity_immutable,
)

# -------------------------------------------------------------------
# item_tools
# -------------------------------------------------------------------
from item_tools import (
    equip_item,
    attune_item,
    use_expendable_resource,
    use_font_of_magic,
    manage_inventory,
    generate_random_loot,
    encode_new_compendium_entry,
)

# -------------------------------------------------------------------
# map_tools
# -------------------------------------------------------------------
from map_tools import (
    manage_map_geometry,
    manage_map_terrain,
    manage_map_trap,
    discover_trap,
    manage_light_sources,
    ingest_battlemap_json,
    create_illusion_wall,
    investigate_illusion,
    reveal_illusion,
)

# -------------------------------------------------------------------
# spatial_tools
# -------------------------------------------------------------------
from spatial_tools import (
    place_entity,
    move_entity,
    manage_skill_challenge,
    get_entity_space,
)

# -------------------------------------------------------------------
# combat_flow_tools
# -------------------------------------------------------------------
from combat_flow_tools import (
    start_combat,
    update_combat_state,
    end_combat,
)

# -------------------------------------------------------------------
# narrative_tools
# -------------------------------------------------------------------
from narrative_tools import (
    create_storylet,
    list_active_storylets,
    request_graph_mutations,
    sync_knowledge_graph,
    run_ingestion_pipeline_tool,
    hydrate_campaign,
    hydrate_delta,
    hydrate_compendium,
    set_storylet_deadline,
    get_scene_provenance,
    propose_backstory_claim,
    review_backstory_claims,
    approve_backstory_claim,
    reveal_secret,
    detect_missing_entities,
)

# -------------------------------------------------------------------
# knowledge_tools
# -------------------------------------------------------------------
from knowledge_tools import (
    query_bestiary,
    query_rulebook,
    query_campaign_module,
    get_creature_tactics,
)

# -------------------------------------------------------------------
# world_tools
# -------------------------------------------------------------------
from world_tools import (
    roll_generic_dice,
    search_vault_by_tag,
    advance_time,
    refresh_vault_data,
    report_rule_challenge,
    perform_ability_check_or_save,
    take_rest,
    interrupt_rest,
    travel,
    use_heroic_inspiration,
    perform_group_check,
    perform_social_interaction,
    convert_currency,
    parse_and_format_coins,
    evaluate_encounter_difficulty,
    build_encounter,
    distribute_encounter_xp,
    generate_or_calibrate_encounter,
    calculate_carrying_capacity,
    sell_item,
    deduct_lifestyle_expense,
    check_craft_prerequisites,
    calculate_crafting_time,
    record_crafting_progress,
)
