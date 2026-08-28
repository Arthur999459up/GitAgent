# Prepare an Issue Fix Guide

The user has agreed to continue from Issue analysis to candidate generation.

## Issue

<issue>
{{issue}}
</issue>

## Repository evidence and conversation

<observations>
{{observations}}
</observations>{{guidance}}

## Output contract

- `description`: a concrete, concise account of the supported root cause, intended behavior change, implementation direction, and required regression coverage.
- `target_files`: the minimal repository paths supported by the Issue or repository evidence. Include a relevant test path only when the evidence identifies it.
- `suggested_title`: a short, specific Draft PR title.

Do not invent file contents, unsupported paths, or APIs, and do not claim that a patch has been generated, applied, or tested.
