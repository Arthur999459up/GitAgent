# Explain a Code Change

## Request

{{request}}

## Repository evidence

<evidence>
{{evidence}}
</evidence>

## Requirements

- Explain only behavior supported by the evidence; label uncertainty or missing context explicitly.
- Identify relevant symbols and call relationships only when they are visible or directly implied by the supplied code.
- Describe impact scope concretely in terms of files, components, callers, or user-visible behavior.
- Do not propose or perform a GitHub write.

## Output contract

Populate `behavior_changes`, `key_symbols`, `call_relationships`, and `impact_scope`.{{guidance}}
