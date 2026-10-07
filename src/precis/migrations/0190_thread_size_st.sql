-- thread_size admitted only metric designations, yet the iso-14585 /
-- iso-14586 series (Torx tapping screws) serve ST2.9-ST6.3 rows whose
-- specs carry thread_size='ST..'. Admit exactly the series' set.
UPDATE component_specs
   SET allowed_values = '["M3", "M4", "M5", "M6", "M8", "M10", "M12", "M16", "M20",
                          "ST2.9", "ST3.5", "ST4.2", "ST4.8", "ST5.5", "ST6.3"]'::jsonb
 WHERE spec_id = 'thread_size';
