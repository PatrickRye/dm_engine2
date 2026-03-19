from typing import TypedDict, Annotated, List, Optional
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class DMState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    vault_path: str
    active_character: str
    draft_response: str
    qa_feedback: str
    revision_count: int


class QAResult(BaseModel):
    approved: bool = Field(description="True if the draft strictly follows all DM rules.")
    feedback: str = Field(description="If approved, write 'APPROVED'. If false, give exact fix instructions.")
    requires_clarification: bool = Field(
        default=False,
        description=(
            "True if the player's input is confusing or if a tool was executed "
            "incorrectly and the engine needs to ask the player to clarify."
        ),
    )
    clarification_message: str = Field(
        default="",
        description="If requires_clarification is True, write an OOC message directly to the player asking them to clarify.",
    )


class Describable(BaseModel):
    """Base model for any entity that can be described with text."""

    appearance: str = Field(default="", description="Physical description. IMPROVISE if not provided.")
    current_appearance: str = Field(default="", description="Current situational appearance. IMPROVISE.")
    icon_url: str = Field(
        default="",
        description=(
            "Relative vault path to the top-down token/avatar image for the VTT map " "(e.g., 'server/Compendium/tokens/goblin.png')."
        ),
    )
    misc_notes: str = Field(default="", description="Catch-all for extra lore, rumors, dark secrets, or 'Jazz'.")


class Social(Describable):
    """For any entity with social characteristics, goals, and connections."""

    long_term_goals: str = Field(default="", description="Overarching life goals. IMPROVISE if not explicit.")
    immediate_goals: str = Field(default="", description="What the entity wants right now. IMPROVISE.")
    aliases_and_titles: str = Field(default="", description="Known aliases/titles. IMPROVISE if appropriate.")
    connections: str = Field(default="", description="Known allies, enemies, or affiliations.")


class CharacterDetails(Social):
    """Core attributes for any character, PC or NPC."""

    base_attitude: str = Field(default="", description="Default demeanor. IMPROVISE.")
    dialect: str = Field(default="", description="Accent and vocabulary. IMPROVISE.")
    mannerisms: str = Field(default="", description="Physical or verbal quirks. IMPROVISE.")
    code_switching: str = Field(
        default="",
        description="Can they adapt their accent/mannerisms to fit into different social classes or situations? IMPROVISE.",
    )


class Feature(BaseModel):
    name: str
    level: int
    description: str
    mechanics_entry: Optional[str] = None


class ClassLevel(BaseModel):
    class_name: str
    level: int
    subclass_name: Optional[str] = None


class ClassDefinition(BaseModel):
    name: str
    features: List[Feature] = Field(default_factory=list)


class SubclassDefinition(ClassDefinition):
    parent_class: str


class PCDetails(CharacterDetails):
    """Specific details for Player Characters, including their full stat block."""

    species: str = Field(default="", description="Character species/race.")
    background: str = Field(default="", description="Character background.")
    classes: List[ClassLevel] = Field(default_factory=list)
    hp: int = Field(default=10, description="Hit points.")
    ac: int = Field(default=10, description="Armor Class.")
    strength: int = Field(default=10, description="Strength score (1-30).")
    dexterity: int = Field(default=10, description="Dexterity score (1-30).")
    constitution: int = Field(default=10, description="Constitution score (1-30).")
    intelligence: int = Field(default=10, description="Intelligence score (1-30).")
    wisdom: int = Field(default=10, description="Wisdom score (1-30).")
    charisma: int = Field(default=10, description="Charisma score (1-30).")
    spells: dict = Field(default_factory=dict, description="List of known spells, cantrips, and spell slots.")
    proficiencies: str = Field(default="", description="Known skills, tool...")

    @property
    def character_level(self) -> int:
        return sum(c.level for c in self.classes)


class NPCDetails(CharacterDetails):
    """Specific details for Non-Player Characters."""

    stat_block: str = Field(default="", description="Standard 5e stat block. MUST GENERATE if not provided.")
    legendary_actions_max: int = Field(default=0, description="Max legendary actions per round.")
    legendary_actions: List[str] = Field(default_factory=list, description="List of legendary actions.")
    lair_actions: List[str] = Field(default_factory=list, description="List of lair actions.")


class LocationDetails(Social):
    """Details for geographical locations."""

    demographics: str = Field(default="", description="Population makeup and culture. IMPROVISE.")
    key_features_and_landmarks: str = Field(
        default="", description="Notable architecture or geographical features. IMPROVISE."
    )
    government: str = Field(default="", description="Local leadership and laws. IMPROVISE.")
    establishments: str = Field(default="", description="Taverns, shops, and points of interest. IMPROVISE.")
    diversity: str = Field(
        default="",
        description=(
            "The specific types of folk, races, or factions players might encounter "
            "here. Is it a homogeneous or diverse melting pot? IMPROVISE."
        ),
    )


class FactionDetails(Social):
    """Details for factions, guilds, or other organizations."""

    goals: str = Field(default="", description="Faction's primary objectives.")
    assets: str = Field(default="", description="Faction's resources or strongholds.")
    key_npcs: str = Field(default="", description="Notable leaders or members.")


class EntityDetails(PCDetails, NPCDetails, LocationDetails, FactionDetails):
    """
    A comprehensive model that includes all possible entity fields.
    This remains for backward compatibility and for tools that need to handle any entity type.
    When creating a new entity, prefer using the more specific models like PCDetails, NPCDetails, etc.
    """

    pass
