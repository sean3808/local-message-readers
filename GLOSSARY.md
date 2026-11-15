# Local Message Readers

This is the glossary for reading messages from chat applications. For the project's positioning see `AGENTS.md`; for the scope boundary see `docs/adr/0003-email-out-of-scope.md`.

## Language

**Conversation**:
A series of message exchanges on a single channel, identified by that platform's native id. One-to-one and group exchanges are the same concept, told apart by an attribute rather than a separate noun. Conversations are never merged across channels: the same person on two platforms is two conversations.
_Avoid_: chat, chat room, session, thread, room

**Channel**:
A chat platform that is read, for example LINE, WeChat, Teams.
_Avoid_: platform, service, source, provider, app

**Reader**:
A command-line program for a single channel that writes that channel's conversations and messages to stdout as JSON. It is read-only toward the channel: it does not modify the channel's data and does not change the read state (unread / read); it may write to the local archive and to temporary snapshots that exist while it runs.
_Avoid_: connector, integration, client, plugin

**Local Agent**:
The consumer of a reader's output: an agent that can open a shell. This is the only audience a reader serves by default.
_Avoid_: assistant, bot, consumer, caller

**Upstream tool**:
A program maintained by a third party that does the actual reading work. This repo does not rewrite it, it only calls it.
_Avoid_: dependency, vendor, library, third-party

**Adapter**:
A thin layer in this repo that normalizes the output of an upstream tool into the vocabulary defined in this document.
_Avoid_: wrapper, shim, bridge, facade

**Archive**:
The cumulative store of messages that this project maintains itself, shared across channels. Once a message is written it does not disappear because the platform purged its cache or the computer or phone was replaced. Each message records the channel and conversation it belongs to, and notes where the data was read from.
_Avoid_: backup, cache, database, save file

**Read source**:
Which kind of data an archived message was read from, for example the local Teams cache or the official Teams export file. One channel can have several read sources; "same read source" means different reads of the same kind of data, not the same channel.
_Avoid_: source (confused with Channel), source platform, provider

## Verification terms

**Reference conversation**:
A conversation whose content is known and under the user's own control, used as the control group to verify that a reader reads correctly.
_Avoid_: fixture, test data, sample

**Reconciliation**:
Comparing a reader's output item by item against the channel's own export file, to confirm nothing is missing, out of order or altered. The two datasets are produced independently, so they can verify each other.
_Avoid_: validation, verification, diff, comparison

**Screen check**:
Comparing a reader's output with the messages displayed on the channel application's screen. It can only spot-check what the screen has loaded, so it is weaker than reconciliation and cannot replace it.
_Avoid_: reconciliation, UI verification
