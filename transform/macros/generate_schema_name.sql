{#
    Use the schema a model declares, verbatim.

    dbt's default behaviour concatenates the target schema with the
    model's custom schema, so a model configured with `+schema: silver`
    against target schema `public` would land in `public_silver`. This
    project already owns real `bronze` / `silver` / `gold` schemas
    created in sql/init/, and the medallion layer names are part of the
    contract with everything downstream — dbt must not rename them.
    Jinja2 Code copied from dbts default macro (CM).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
