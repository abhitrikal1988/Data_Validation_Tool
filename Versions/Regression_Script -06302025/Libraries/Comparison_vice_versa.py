import pandas as pd
from datetime import datetime

def compare_data(source_data, target_data, join_fields, compare_fields, viceversa=False):

    def try_parse_date(val):
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, str):
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(val, fmt).date()
                except ValueError:
                    continue
        return val

    def sort_dataframe(df, keys):
        return df.sort_values(by=keys).reset_index(drop=True)

    def run_comparison(src_df, tgt_df, join_fields, field_map, direction="SRC→TGT"):
        src_keys, tgt_keys = [], []
        for join in join_fields:
            for k, v in join.items():
                if k.startswith("SRC"):
                    src_keys.append(v)
                elif k.startswith("TGT"):
                    tgt_keys.append(v)

        tgt_df_renamed = tgt_df.rename(columns={tgt: src for src, tgt in zip(src_keys, tgt_keys)})
        src_df = sort_dataframe(src_df, src_keys)
        tgt_df_renamed = sort_dataframe(tgt_df_renamed, src_keys)

        merged = pd.merge(src_df, tgt_df_renamed, on=src_keys, how='outer', suffixes=('_src', '_tgt'), indicator=True)

        results = []
        for _, row in merged.iterrows():
            join_key_vals = {k: row.get(k) for k in src_keys}

            if row['_merge'] == 'left_only':
                results.append(f"[{direction}] Missing in Target: {join_key_vals}")
            elif row['_merge'] == 'right_only':
                results.append(f"[{direction}] Missing in Source: {join_key_vals}")
            else:
                for f in field_map:
                    s_field, t_field = None, None
                    for k, v in f.items():
                        if k.startswith("SRC"):
                            s_field = v
                        elif k.startswith("TGT"):
                            t_field = v

                    s_val = try_parse_date(row.get(s_field))
                    t_val = try_parse_date(row.get(t_field))

                    if pd.isna(s_val) and pd.isna(t_val):
                        continue
                    if s_val != t_val:
                        results.append(
                            f"[{direction}] Mismatch at {join_key_vals}: {s_field}={row.get(s_field)} ≠ {t_field}={row.get(t_field)}"
                        )
        return results

    # Forward comparison
    src_df = pd.DataFrame(source_data)
    tgt_df = pd.DataFrame(target_data)
    logs = run_comparison(src_df, tgt_df, join_fields, compare_fields, direction="SRC→TGT")

    # Reverse comparison if enabled
    if viceversa:
        # Flip join fields and compare fields
        reversed_joins = [{k.replace("SRC", "TGT") if k.startswith("SRC") else k.replace("TGT", "SRC"): v for k, v in join.items()} for join in join_fields]
        reversed_fields = [{k.replace("SRC", "TGT") if k.startswith("SRC") else k.replace("TGT", "SRC"): v for k, v in field.items()}for field in compare_fields]
        logs += run_comparison(tgt_df, src_df, reversed_joins, reversed_fields, direction="TGT→SRC")

    # with open(log_file, "w", encoding="utf-8") as f:
    #     f.write("\n".join(logs))
    # print(f"Comparison done. {len(logs)} issues logged in '{log_file}'.")
