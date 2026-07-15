# Improvements plan

Implicit goal is to keep all docs upto date and cohesive.

## Selection Buttons for Open Library Are Poor.

They require you to type in a folder path, then press open.
It would be nice if it opened up the OS level selector for a folder path or file path.
Unsure how web apis interact with this.

## UI is Too Sparse

The UI needs to be dense.
We should note this in design requirements somewhere.
Right now everything is splayed out everywhere over multiple tabs and whatnot.
We want to try to make sure that information density is high. Particularly for conversion workflows.
Make use of dense tables and hover text.
You can hard assume desktop viewport, optimize for that.

## UI / UX

Problems:
- The flow is jank
- Select all / Unselect all?
- Only summary recipe is shown?
- How do I run all recipies on a paper? I feel like I should be able to select which recipies to run as a checklist.
- No idea whats going on when the LLM stuff is running.

Nice to have:
- Sorting the papers by name or key or year or whatnot.

## LLM Spend

No indication of LLM spend anywhere on the dashboard or in the logs or anything.
No idea if cache hits worked or anything.
Should probably be tracked in schema.

Caching also doesn't seem to work.
It might be due to some sort of OpenAI now charging for cache writes.

## The installation and running is confusing

At least remove llm as an optional thing.
The library is literally useless if neither marker nor llm is installed.

## Remote computer for conversions

I would like to use a remote computer for conversions.
My laptop can't really handle marker on this many papers, but I have access to an ubuntu 24 serve with a dedicated GPU (RTX 3090)
accessible via ssh (`ssh noesis`).

I don't understand how to do this, its not clearly documented nor am I sure this is even supported.
Would this require me to run the dashboard on the remote computer?

## Library Schema

The schema could use improvents.
Keep in mind that we want to minimize the number of hops for LLMs to get the information they want.
The second goal would be to minimize wasted tokens when they do find what they are trying to read.

### Structure

We want to keep things flat.
So generated stuff should be spat directly into the main folder.

Ex:
papers\ashWarmStartingNeuralNetwork2020\transcribed.md or whatever.

Things that can have heirarchy are things that we want the agents to ignore or things that are a large number of similar files.
Specifically:

- pdf page images.
- run logs.
- figure images.
- source files.


### Frontmatter

Try not to put frontmatter into files. We can keep it in logs or jsons.
Example this generated file contains this frontmatter:

```
---
generated_by: paper-pipeline
recipe: contributions
recipe_version: 2
provider: openai
model: gpt-5.6-luna
input: papers/ashWarmStartingNeuralNetwork2020/source/11a9fb01d6cef46c478399fe6c89352cfc2a94624f074cd960caa5840086813f.pdf
input_sha256: 11a9fb01d6cef46c478399fe6c89352cfc2a94624f074cd960caa5840086813f
created: 2026-07-15T17:37:01Z
---
```

We don't need this. Its useful to track stuff like this (including API spend).
But AGENTS do not need this when just attempting to read contributions.md, its just wasted tokens.
So keep it to json.


### Source Directory

We don't really need a source directory.
Just keep any and all meta paper files contained in one place.
Probably in the pp folder.

