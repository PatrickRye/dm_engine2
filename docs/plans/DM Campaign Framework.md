# Computational Narratology and Agentic Orchestration: Transforming TTRPG Source Material into Dynamic Narrative Data Structures

The translation of static tabletop role-playing game (TTRPG) source materials into dynamic, machine-navigable architectures represents a profound challenge at the intersection of computational narratology, artificial intelligence, and interactive systems design. Traditional linear media operates on a fixed chronological sequence of events, but interactive tabletop environments demand a delicate equilibrium between authorial intent and participant agency. When an artificial intelligence acts as the orchestrator or Game Master (GM), it must possess the capacity to interpret rigid source material—ranging from strictly sequenced campaign plots to open-ended worldbuilding guides detailing lands, factions, politics, and villains—not as a predetermined script, but as a topological space of narrative possibilities.

The objective is to synthesize a computational framework capable of ingesting varied source texts, structuring them into queryable data models, and dynamically deploying them during runtime. This requires examining established best practices from expert human facilitators, insights from improvisational theater, and advanced data structuring paradigms such as directed acyclic graphs (DAGs), knowledge graphs, and storylet-based narrative engines. Furthermore, to prevent an autonomous system from generating thematic dissonance or breaking the internal logic of the established world, robust deterministic boundaries—often termed "hard guardrails"—must be encoded directly into the architecture. By mapping human creativity onto rigorous data structures, it becomes possible to design an AI agent that allows participants to live within a vibrant, guided world without feeling artificially constrained by the underlying source material.

## The Illusion of Linear Plots and Node-Based Scenario Architectures

The foundational error in adapting narrative for interactive systems is the reliance on the plotted approach. A plot is inherently a linear sequence of events attempting to predetermine outcomes that have not yet occurred. When translated into interactive media, this approach frequently manifests as the "Choose Your Own Adventure" model, which inevitably succumbs to an exponential explosion of contingencies. If a designer attempts to account for every possible participant choice, the required computational and authorial overhead expands unsustainably, resulting in a fragile system where unpredicted choices break the narrative progression. This creates severe chokepoints—single points of failure where an interactive system grinds to a halt because participants missed a clue, failed a skill check, or made an unanticipated decision.

A far more resilient paradigm, critical for machine orchestration, is node-based scenario design, which advocates for preparing situations rather than plots. In this framework, the narrative is conceptualized as a collection of nodes—locations, events, characters, or clues—interconnected by non-linear pathways. This aligns with the philosophy that game masters should design the map and the actors, but never the exact path the participants will take. A critical mechanism within this paradigm is the Three Clue Rule, which dictates that for any essential conclusion the participants must reach to advance the narrative, the architecture must provide at least three distinct vectors of discovery. This redundancy eliminates narrative chokepoints and provides the AI orchestrator with multiple fallback options when participants deviate from expected behavior.

Furthermore, the Inverted Three Clue Rule posits that if participants are provided access to any three distinct clues pointing to different nodes, they will inevitably engage with at least one, driving the narrative forward without requiring systemic coercion or railroading. When an AI agent manages a campaign, operating within a node-based architecture allows the system to act fluidly. Rather than forcing participants down a singular path, the AI monitors the current node, assesses the available outbound vectors, and seamlessly adjusts the state of the surrounding unvisited nodes based on participant actions. The transition between these nodes is governed by narrative "gravity," which consists of the "pulls" (rewards or clues drawing participants toward a node) and "pushes" (threats or events forcing them into a node). By dynamically adjusting pushes and pulls, an AI can maintain narrative momentum while preserving the illusion of total participant freedom.

## Expert Human Methodologies and Character-Driven Worldbuilding

To imbue an AI agent with the flexibility of an expert human GM, one must examine the methodologies of prolific facilitators such as Brennan Lee Mulligan. Mulligan’s approach heavily prioritizes character-driven worldbuilding over rigid environmental simulation. Where traditional simulationist design sets the scene first, establishing geography and history independent of the participants, this improvisational approach sets the narrative stakes first. World generation becomes highly reactive; cities, pantheons, and political factions are instantiated or detailed precisely when they intersect with participant backstories or mechanical choices. If no participant chooses to play a cleric, the underlying pantheon of gods remains largely undefined, preventing wasted computational and authorial effort.

This methodology mirrors the "Yes, and" philosophy of improvisational theater. When generating non-player characters (NPCs) on the fly, Mulligan often eschews complex internal logic in favor of distinct, dial-based behavioral parameters, sometimes conceptualized as "bubbles" of traits or limited-use abilities. For instance, an NPC might be defined entirely by two vector dimensions: "devotion to a sibling" and "extreme whimsy". By adjusting the weights of these two parameters in response to participant input, the facilitator generates consistent, engaging, and dynamic responses without requiring a deeply simulated, computationally expensive psychological profile. All character actions simply become functions of these weighted values.

Furthermore, establishing a "relationship web" is vital for dynamic storytelling. Rather than viewing NPCs and factions in isolation, the AI must understand the interconnected tissue between them. By mapping out a relationship web early in the campaign—often explicitly defining how each participant character feels about specific NPCs and vice versa—the orchestrator gains instant plot hooks. When an event occurs at one node in the web, the tension reverberates through the interconnected threads, allowing the AI to organically generate subsequent encounters and conflicts based on pre-established emotional resonances rather than arbitrary plot requirements.

|**Facilitation Technique**|**Human Implementation**|**Computational/AI Equivalent**|
|---|---|---|
|**Stake-Setting**|Focusing on character motivations over environmental descriptions.|Prioritizing narrative state-space evaluations over dense environmental generation.|
|**Dial-Based NPCs**|Boiling character personalities down to two or three core, adjustable traits.|Vector embeddings with weighted behavioral parameters governing LLM generation.|
|**Reactive Worldbuilding**|Inventing locations and lore solely based on participant class and backstory choices.|Lazy evaluation; generating Knowledge Graph nodes only when queried by the active narrative path.|
|**Relationship Webs**|Charting emotional connections between PCs and NPCs to generate organic drama.|Storing relationships as semantic edges in a graph database, utilized for traversal and event triggering.|

## Improvisational Frameworks and Interactive Literature Paradigms

The translation of these TTRPG techniques into a systemic architecture requires grounding in the theories of improvisational theater and interactive literature. The macro-structure of long-form improvisational theater, specifically "The Harold," provides a compelling blueprint for dynamic storytelling. Developed by Del Close and Charna Halpern, The Harold eschews traditional linear narrative, beginning instead with divergent, seemingly unrelated scenes generated from a single thematic prompt. As the performance continues, these disparate strands cross-pollinate, echo previous themes, and eventually converge in a unified resolution. In computational terms, this is analogous to an agglomerative clustering algorithm operating within a narrative state space. The AI can be programmed to initialize disparate plot threads (nodes) and dynamically forge semantic edges between them as the runtime progresses, ensuring that participant actions in one narrative bubble eventually ripple into others to create a cohesive whole.

However, improvisation also requires the strict maintenance of the established reality, often referred to in theater as the "fishbowl". If the fundamental physics, logic, or thematic tone of the environment is violated—if an actor drops the imaginary fishbowl they established at the start of the scene—the audience's suspension of disbelief shatters irreparably. For an AI orchestrator managing a campaign, this necessitates the implementation of strict boundaries to maintain the integrity of the source material. The AI must be constrained by the established rules of the world, preventing it from generating physically impossible or thematically incongruous events.

This tension between freedom and constraint is central to the study of interactive literature. Janet Murray, in _Hamlet on the Holodeck_, identifies four core affordances of digital storytelling mediums: they are procedural, participatory, encyclopedic, and spatial. An AI GM leverages the encyclopedic affordance by holding the entirety of a campaign's lore in memory, but it must use the procedural affordance to generate meaningful responses to the participatory actions of the players. Chris Crawford's models of interactive storytelling further define this as a continuous loop of "Listen, Think, Speak". An effective AI system cannot merely execute a pre-written multilinear branching path; it must exhibit true responsiveness, where the system's affordances and feedback change drastically as a direct result of participant agency, validating their impact on the narrative world.

## Data Structures for Interactive Narrative Orchestration

To operationalize these narratological and improvisational theories, the raw text of source materials must be translated into highly specific computational data structures. The choice of structure fundamentally dictates the capabilities, limitations, and expressive range of the AI agent navigating it. Various paradigms have been tested in the realm of interactive storytelling, each offering distinct advantages depending on the nature of the source material.

## Directed Acyclic Graphs (DAGs) and Finite State Machines

Historically, interactive narratives have relied on Finite State Machines (FSMs) and Directed Acyclic Graphs (DAGs) to map out player progression. In a DAG, narrative beats are represented as vertices, and the transitions between them are directed edges. The acyclic nature ensures that the narrative always moves forward toward a conclusion, preventing infinite loops and ensuring a coherent pacing structure.

While DAGs are excellent for mapping strictly linear campaign modules or dungeon crawls—where the sequence of events is highly constrained and physical geography limits movement—they struggle profoundly to handle the open-ended nature of tabletop world guides. A DAG cannot easily accommodate backtracking, sandbox exploration, or the emergent recombination of narrative elements. When a DAG is utilized for complex narratives, the AI acts primarily as a rigid traffic controller, routing participants through predefined pathways based on simple boolean logic, severely limiting the feeling of a living world.

## Knowledge Graphs and Semantic Networks

For open-ended world guides detailing lands, factions, politics, and villains, Knowledge Graphs (KGs) represent a vastly superior paradigm. A knowledge graph stores information as interconnected triples (Subject, Predicate, Object), creating a dense, multi-dimensional web of relationships. For example, a KG can encode that `(Faction A) --> (Faction B)`, and that `(NPC X) --> (Faction A)`.

When an AI agent is equipped with a Knowledge Graph via Graph Retrieval-Augmented Generation (GraphRAG), it possesses a deterministic, structured memory of the entire world state. Unlike standard vector embeddings, which rely purely on semantic similarity and can easily hallucinate relationships or forget distant contexts, GraphRAG allows the AI to perform multi-hop logical deductions. If the participants assassinate NPC X, the AI can traverse the graph to determine exactly which factions, locations, and subsequent plots are impacted, thereby facilitating deep, systemic world reactivity. The knowledge graph acts as an objective truth engine, preventing the LLM from inventing contradictory lore.

## Storylets and Quality-Based Narratives

The most robust data structure for combining narrative progression with open-world flexibility is the Storylet architecture. Pioneered in quality-based narrative systems, a storylet decouples narrative events from chronological sequencing. Instead of residing at a fixed point on a branching tree, a storylet floats in a latent, multi-dimensional space and is entirely self-contained.

A formal storylet consists of three vital components:

1. **Prerequisites (Conditions):** A set of logical checks against the global world state (the Knowledge Graph) that must be true for the storylet to become available. For instance, a storylet might require `Participant_Level > 4` AND `Possesses_Amulet == True` AND `Location == "Dark Forest"`.
    
2. **Content:** The actual narrative text, dialogue, or encounter data presented to the participants.
    
3. **Effects (Mutations):** The adjustments made to the global world state upon the storylet's conclusion. This could include boolean flips, integer adjustments, or graph mutations (e.g., `Faction_Reputation -= 10`, `Has_Met_Villain = True`).
    

This structure elegantly eliminates the exponential branching problem. The AI orchestrator does not need to compute or traverse an entire decision tree; it simply polls the database of storylets at each runtime step, filters for those whose prerequisites are met by the current world state, and selects the most narratively appropriate option to present to the participants.

## Comparative Analysis of Narrative Data Structures

To determine the optimal architecture for an AI orchestrator, the following parameters define the operational efficacy of each structure when applied to tabletop source material:

|**Data Structure**|**Flexibility & Emergence**|**Temporal Sequencing**|**Best Suited Source Material**|**AI Orchestration Complexity**|
|---|---|---|---|---|
|**Directed Acyclic Graph (DAG)**|Low (Highly constrained)|High (Strictly enforced chronology)|Linear Campaign Modules, Dungeon Crawls|Low (Simple boolean pathfinding)|
|**Finite State Machine (FSM)**|Low to Moderate|Moderate (Allows chronological loops)|Combat Encounters, Mechanical Puzzles|Low (Trigger-based transitions)|
|**Knowledge Graph (KG)**|Very High (Purely relational)|None (Requires external narrative engine)|World Guides, Lore, Faction Politics|High (Requires GraphRAG integration)|
|**Storylet Architecture**|High (Highly emergent)|Emergent (State-dependent)|Dynamic Campaigns, Character Arcs|Moderate (Continuous state polling)|

## Proposing the Optimal Solution: The Graph-Grounded Storylet Orchestrator

To fulfill the mandate of translating both plot-driven campaigns and open-ended world guides into a dynamic but guided storytelling experience, relying on a single data structure is insufficient. The optimal solution for an AI agent is a hybrid architecture: **The Graph-Grounded Storylet Orchestrator**.

This comprehensive solution synthesizes the relational depth of a Knowledge Graph with the narrative pacing of a Storylet engine, overseen by a high-level Drama Manager agent that emulates the intuition of an expert human facilitator.

## Component 1: The Knowledge Graph as the World State Ontology

The world guide—encompassing lands, factions, NPCs, and historical events—is ingested and parsed into a comprehensive Knowledge Graph. This graph serves as the absolute, objective reality of the game world. It tracks the spatial topology (which towns connect to which forests), the sociopolitical web (who hates whom), and the inventory of critical artifacts. The graph ensures that the AI possesses perfect, hallucination-free recall of the environment, solving the context-window limitations that plague standard LLM deployments.

## Component 2: Storylets as the Narrative Vector Space

The campaign plots, specific quests, and villain schemes are fractured from their linear presentation and converted into modular Storylets. Instead of relying on simple boolean flags, the prerequisites for these storylets are written as complex Graph Queries. For example, a storylet depicting an ambush by a specific villain only becomes active if the Knowledge Graph confirms that a traversable path exists between the villain's current location node and the participants' current location node. This grounds the narrative beats in the simulated physical reality of the world.

## Component 3: The Drama Manager Agent

With dozens of storylets potentially valid at any given moment due to the open-ended nature of the world, the AI utilizes a Drama Manager algorithm to select the optimal narrative beat. Drama Managers are frequently modeled using reinforcement learning (RL) or Markov Decision Processes (MDPs) to track player intent and narrative momentum.

The Drama Manager assesses the current "tension arc" of the session. If the participants have experienced prolonged, low-stakes exploration, the Drama Manager prioritizes the selection of a high-stakes, conflict-driven storylet to escalate the drama. This preserves participant agency—because they are free to navigate the world and trigger any valid conditions—while allowing the AI to subtly guide the pacing and ensure the overarching plot is advanced. By defining NPCs with simplistic, dial-based parameters (emulating Mulligan's "bubbles" of trait weights ), the Drama Manager can dynamically instantiate conversational storylets that feel deeply grounded in character motivation without requiring computationally expensive psychological simulations.

## Implementing "Hard Guardrails" for Thematic Consistency

A persistent vulnerability in generative AI orchestration is narrative drift, often referred to as the "thousand cuts," where minor semantic hallucinations slowly accrue until the output violates the core themes or rules of the source material. To prevent an AI from transforming a grim, high-lethality dark fantasy campaign into a whimsical, consequence-free adventure, or allowing participants to circumvent central plot constraints through manipulative prompting, the system must enforce strict boundaries.

Guardrails in AI systems are categorized into two primary types: soft guardrails and hard guardrails.

Soft guardrails rely on systemic prompting, cultural alignment within the Large Language Model context window, and behavioral guidelines (e.g., instructing the AI to "maintain a dark and brooding tone at all times"). While useful for linguistic styling and surface-level tone management, soft guardrails are inherently probabilistic and prone to catastrophic failure under participant pressure, complex edge cases, or extended context lengths.

Hard guardrails, conversely, are deterministic, algorithmic constraints executed entirely outside the LLM's neural network. In the context of the Graph-Grounded Storylet Orchestrator, hard guardrails function as absolute physical and thematic laws that the AI cannot override.

## Mechanisms of Thematic Guardrails

To ensure the AI agent operates safely within the confines of the established world and campaign themes, three specific mechanisms of hard guardrails must be implemented within the architecture:

1. **State-Space Validation:** Before the AI presents any generated narrative prose, mechanical outcome, or dialogue to the participants, the output must pass through a programmatic validation layer that cross-references the Knowledge Graph. If the AI generates text stating that a king has granted the participants a legendary magical sword, the validation layer executes a graph query to confirm the king actually possesses the sword in the current world state. If the edge `(King) --> (Sword)` does not exist, the output is blocked, and the AI is forced to regenerate the response. This eliminates hallucinated rewards and sequence breaking.
    
2. **Immutable Node Attributes:** Thematic consistency is preserved by assigning immutable metadata tags to certain nodes and storylets within the database. A primary antagonist might possess the tag ``. Even if participants roll exceptionally well on persuasive mechanical actions or provide incredibly clever conversational input, the hard guardrail intercepts the logic flow, preventing the LLM from generating an outcome where the villain peacefully surrenders or becomes an ally. The AI is structurally forced to route the outcome into combat, escape, or deception, but never diplomatic capitulation.
    
3. **Privilege Segregation via Composable AI:** Utilizing a multi-agent architecture ensures that the LLM responsible for generating the creative prose does not have direct write-access to the Knowledge Graph. A specialized, highly constrained Logic Agent interprets the narrative output, translates it into graph mutations, and rigorously verifies those mutations against the campaign's thematic rulebook before committing the update. This prevents the creative engine from accidentally deleting core world constraints or corrupting the relationship web during moments of high token generation.
    

These hard guardrails act as the invisible walls of the improv "fishbowl," guaranteeing that participant agency operates within a meticulously curated thematic playground. The system is dynamic, yet fundamentally anchored to the source material.

## Algorithmic Instantiation from Source Material

To bring this complex architecture to life, a formalized, automated pipeline is required to ingest raw text from PDF source books, campaign wikis, or digital modules, and output the structured Graph-Grounded Storylet database. Because campaign books (which are highly plot-driven) and world guides (which are open-ended) feature fundamentally different topologies, the ingestion process must be divided into two distinct, specialized algorithms.

## Pipeline 1: Converting World Definition Source Material into a Relational Knowledge Graph

World guides consist of gazetteers, encyclopedias of gods, lists of taverns, and extensive faction histories. The primary goal of this algorithm is to extract entities and map their relational topologies.

**Phase 1: Entity Extraction and Classification** Let the source text $T$ be segmented into coherent chunk arrays. A specialized Natural Language Processing (NLP) model—potentially leveraging models like RT-DETR for layout parsing if extracting from formatted documents—extracts a set of nodes $V$, where each node $v \in V$ is classified by type (e.g., Location, NPC, Faction, Item, Deity).

- _Heuristic Filter:_ The system applies Named Entity Recognition (NER) tuned specifically for fantasy, science fiction, or relevant genre lexicons to prevent common nouns from being falsely classified as unique nodes. Furthermore, entity disambiguation algorithms merge duplicate mentions (e.g., ensuring "The Shadow King" and "Emperor Malakor" map to the same node if the text reveals they are the same entity).
    

**Phase 2: Semantic Edge Inference**

For every pair of nodes $(v_i, v_j)$ found within the same contextual window, an LLM evaluates the surrounding text to infer the relationship, generating a set of directed semantic edges $E$.

- If the text reads, "The Shadow Thieves heavily control the smuggling operations on the docks of Waterdeep," the system generates the directed edge: `(Shadow Thieves: Faction) --> (Waterdeep Docks: Location)`.
    

**Phase 3: Parameterization and Dial Assignment** To emulate Brennan Lee Mulligan's character-driven improvisation, the system scans the biographical text associated with each NPC node to extract core personality traits. Using sentiment analysis and keyword extraction, the system assigns continuous behavioral vectors (Mulligan's "bubbles" or dials ).

- An NPC might be assigned $Trait_{alpha} = \text{Greed (0.8)}$ and $Trait_{beta} = \text{Cowardice (0.9)}$. These continuous variables are stored as node attributes and inform the LLM's roleplay generation at runtime, ensuring characters act consistently with their lore.
    

**Phase 4: Guardrail Encoding** The system identifies absolute truths defined in the text (e.g., "The ancient celestial seal can only be broken by royal blood") and encodes them as deterministic logic gates attached to the relevant nodes. This establishes the structural hard guardrails that the AI cannot violate during runtime.

## Pipeline 2: Converting Campaign Source Material into Node-Based Storylets

Campaign modules are highly sequential, often written with the strict assumption that participants will move linearly from Room A, uncover Clue B, and fight Boss C. The algorithm must shatter this linear sequence into modular storylets while preserving the logical dependencies required for the overarching plot to remain coherent.

**Phase 1: Plot Node Identification and Preliminary Sequencing** The algorithm ingests the campaign text and uses a sequence-to-sequence architecture to identify discrete narrative events, encounters, and necessary revelations. Let $S$ be the set of extracted narrative beats. The algorithm attempts to map these onto a preliminary Directed Acyclic Graph (DAG) to establish chronological dependencies. Graph centrality algorithms can be employed to determine which nodes are structurally vital to the campaign's conclusion. This establishes that, for example, the players physically cannot interrogate the cultist until they have first located the cultist's hidden lair.

**Phase 2: Prerequisite Generation and State-Space Mapping** The rigid edges of the preliminary DAG are dissolved and replaced with flexible Storylet Prerequisites. For a target storylet $s_n$, the system analyzes its upstream dependencies in the preliminary DAG.

- Instead of hardcoding a sequence, the system defines the prerequisite as a graph query against the world state: `If [Cultist_Hideout] is AND [Cultist] is [Captured], THEN unlock Storylet (Interrogation)`. This decouples the event from a strict timeline, allowing participants to reach the prerequisite state through emergent, unanticipated gameplay.
    

**Phase 3: Inverse Three Clue Redundancy Generation** This is the most critical computational step for translating linear content into robust interactive content. Linear campaigns notoriously suffer from single points of failure. The algorithm scans the extracted prerequisites for any essential storylet that acts as a narrative bottleneck.

- If Storylet $s_{boss}$ requires the discovery of the `Secret Password`, the algorithm identifies this prerequisite as a highly fragile chokepoint.
    
- Applying the Three Clue Rule , the system prompts a generative LLM to analyze the surrounding Knowledge Graph and mathematically hallucinate _two additional, logically consistent pathways_ to achieve that prerequisite.
    
- The LLM might generate a new, non-canonical storylet where the password can be intimidated out of a cowardly guard, and another where it can be deciphered from a discarded journal in a local tavern. These newly generated storylets are appended to the database, ensuring the architecture is robust, redundant, and highly resilient against participant unpredictability.
    

**Phase 4: Effect Annotation and Reward Distribution** Finally, each storylet is parsed to determine its permanent impact on the world state. The text is analyzed for outcomes, loot distribution, and relationship shifts (e.g., "The goblin leader surrenders and hands over the map"). The algorithm encodes a programmatic effect payload to the storylet: `Execute Graph Mutation: (Participant_Party) --> (Map_Item)` and `Execute Graph Mutation: (Goblin_Faction) --> (Participant_Party)`.

## Synthesis and Future Implications

The successful conversion of tabletop role-playing source materials into machine-navigable formats requires a fundamental shift away from static scripting toward systemic, relational data architectures. By discarding linear plotting in favor of Alexandrian node-based design, and adopting the improvisational principles of character-driven generation and relationship webbing, interactive narratives can achieve unprecedented levels of emergence.

The Graph-Grounded Storylet Orchestrator represents the optimal paradigm for this endeavor. By utilizing a Knowledge Graph to anchor the objective reality of the world, Storylets to manage the causal logic of narrative progression, and a Drama Manager to modulate the tension arc, an AI orchestrator is empowered to navigate the game space with the adaptability and intuition of an expert human facilitator. When fortified with deterministic hard guardrails executed via composable, multi-agent privilege segregation, this architecture ensures that participant agency is maximized without ever compromising the thematic integrity, tone, or physical laws of the established source material. Through automated NLP extraction pipelines and heuristic redundancy generation, vast libraries of legacy TTRPG content can be structurally reimagined, paving the way for a new era of deeply responsive, computationally orchestrated interactive fiction.

# Tasks
# Implementation Task List: Graph-Grounded Storylet Orchestrator

This document outlines the discrete development tasks required to build the AI orchestration system detailed in the underlying research, structured for iterative development and testing.

## Phase 1: Foundational Data Structures

### Task 1.1: Initialize the World State Ontology (Knowledge Graph)

- **Description:** Set up the underlying graph database (or in-memory equivalent) to store entities (nodes) and their semantic relationships (edges).
    
- **Definition of Done (DoD):**
    
    - A graph schema is defined supporting distinct node types (`Location`, `NPC`, `Faction`, `Item`, `Deity`).
        
    - The system can perform basic CRUD operations (Create, Read, Update, Delete) on nodes and directed edges (e.g., `(Faction_A) -[CONTROLS]-> (Location_B)`).
        
    - A basic GraphRAG query function is implemented capable of returning a multi-hop context window for a given node.
        

### Task 1.2: Define the Storylet Data Schema

- **Description:** Create the core data model for a "Storylet" that decouples narrative events from chronological sequences.
    
- **Definition of Done (DoD):**
    
    - A structured schema (e.g., JSON Schema, Pydantic model) exists defining three core components: `Prerequisites` (Graph Queries), `Content` (Text/Prompt), and `Effects` (Graph Mutations).
        
    - The system can successfully validate a mock storylet against this schema.
        
    - A polling function is created that, given a mock global Knowledge Graph state, accurately filters and returns _only_ the storylets whose prerequisites are currently met.
        

## Phase 2: Ingestion Pipeline - World Builder (Open-Ended Content)

### Task 2.1: Entity & Edge Extraction Pipeline

- **Description:** Build the NLP pipeline to process world guide text (lore, gazetteers) into Knowledge Graph nodes and edges.
    
- **Definition of Done (DoD):**
    
    - The pipeline accepts a chunk of lore text and uses an LLM (or NER tool) to extract unique entities without duplicating existing ones (Entity Disambiguation).
        
    - The pipeline infers and formats semantic relationships between entities as valid directed edges.
        
    - Extracted data is successfully committed to the Knowledge Graph.
        

### Task 2.2: NPC Parameterization (Dial Assignment)

- **Description:** Implement the mechanism to assign behavioral dials to NPCs based on their lore descriptions.
    
- **Definition of Done (DoD):**
    
    - The pipeline scans NPC biographical text and outputs 2-3 core continuous variables (e.g., `greed: 0.8`, `loyalty: 0.9`).
        
    - These dials are successfully stored as attributes on the respective NPC nodes in the Knowledge Graph.
        

## Phase 3: Ingestion Pipeline - Campaign Builder (Linear Plot Content)

### Task 3.1: Sequence-to-Storylet Conversion

- **Description:** Parse linear campaign modules into discrete storylets and map their chronological dependencies.
    
- **Definition of Done (DoD):**
    
    - The pipeline extracts distinct narrative beats from a linear text.
        
    - It successfully maps these beats into a preliminary Directed Acyclic Graph (DAG) to establish upstream dependencies.
        
    - It converts these dependencies into flexible `Prerequisite` graph queries for each resulting Storylet.
        

### Task 3.2: Inverse Three Clue Redundancy Generation

- **Description:** Programmatically eliminate narrative chokepoints by generating alternative prerequisite pathways.
    
- **Definition of Done (DoD):**
    
    - The system accurately identifies "bottleneck" storylets (nodes with only one incoming prerequisite edge).
        
    - An LLM routine successfully hallucinates and generates at least two _additional_, logically consistent storylets that satisfy the bottleneck's prerequisite, based on surrounding Knowledge Graph context.
        

### Task 3.3: Effect Annotation Encoding

- **Description:** Analyze storylet outcomes to generate programmatic mutations.
    
- **Definition of Done (DoD):**
    
    - The pipeline parses resolution text (e.g., "The boss drops the key") and translates it into valid Graph Mutations (e.g., `ADD_EDGE (Player) -[POSSESSES]-> (Key)`).
        

## Phase 4: Runtime Orchestration & Guardrails

### Task 4.1: The Drama Manager Agent

- **Description:** Build the selection algorithm that chooses the optimal active storylet to present to the user based on narrative pacing.
    
- **Definition of Done (DoD):**
    
    - The agent maintains a rudimentary "tension arc" state (e.g., low, medium, high tension).
        
    - When presented with multiple valid storylets by the polling function, the agent successfully selects the one that best matches the desired tension trajectory (e.g., selecting a combat storylet after prolonged exploration).
        

### Task 4.2: Privilege Segregation (Multi-Agent Setup)

- **Description:** Separate the system into a Creative LLM Agent (for prose/dialogue) and a strict Logic Agent (for database writes).
    
- **Definition of Done (DoD):**
    
    - The Creative Agent can _read_ from the Knowledge Graph (via GraphRAG) but cannot execute write commands.
        
    - The Creative Agent outputs proposed actions/mutations to the Logic Agent.
        
    - The Logic Agent receives the proposed mutations and formats them for database execution.
        

### Task 4.3: State-Space Validation (Hard Guardrails)

- **Description:** Implement deterministic rules that intercept and validate LLM outputs against the objective Knowledge Graph.
    
- **Definition of Done (DoD):**
    
    - If the Creative Agent proposes an outcome that violates the Knowledge Graph (e.g., giving the player an item they don't have access to), the Logic Agent rejects the mutation.
        
    - The rejection triggers an automatic re-prompt to the Creative Agent to generate a valid response.
        
    - Nodes marked with `Immutable` tags cannot be altered by the Logic Agent under any circumstances.
        

## Phase 5: System Integration & Verification

### Task 5.1: End-to-End Ingestion Test

- **Description:** Verify that raw text can flow through both pipelines into a unified, queryable database.
    
- **Definition of Done (DoD):**
    
    - A sample world text and a sample campaign text are fed into the system.
        
    - The Knowledge Graph populates accurately.
        
    - The Storylet database populates accurately with valid prerequisites tied to the new Knowledge Graph.
        

### Task 5.2: The "Improv Fishbowl" Simulation (Runtime Verification)

- **Description:** Run an automated or semi-automated simulated session to test system resilience and reactivity.
    
- **Definition of Done (DoD):**
    
    - A simulated user submits an unexpected input (e.g., attempting to bypass a planned encounter).
        
    - The Knowledge Graph updates correctly without crashing.
        
    - The Storylet polling engine seamlessly surfaces a different valid narrative node.
        
    - The Drama Manager maintains pacing.
        
    - **Crucially:** The user attempts a "prompt injection" or game-breaking action (e.g., "I persuade the final boss to give up instantly"), and the Hard Guardrails successfully intercept, block, and re-route the interaction.