import pandas as pd
from datetime import datetime

def sort_dataframe(df, keys):
    return df.sort_values(by=keys).reset_index(drop=True)

def try_parse_date(val):
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return val  # return original if not a recognizable date


def compare_data(source_data, target_data, join_fields, compare_fields, log_file="validation_log.txt"):

    try:
        # Convert to DataFrames
        source_df = pd.DataFrame(source_data)
        target_df = pd.DataFrame(target_data)

        # Parse join keys
        source_keys, target_keys = [], []

        source_keys.extend(v for join in join_fields for k, v in join.items() if k.startswith("SRC"))
        target_keys.extend(v for join in join_fields for k, v in join.items() if k.startswith("TGT"))

        # Rename target columns to match source for join
        target_df = target_df.rename(columns={tgt: src for src, tgt in zip(source_keys, target_keys)})

        # Sort both
        source_df = sort_dataframe(source_df, source_keys)
        target_df = sort_dataframe(target_df, source_keys)

        # Merge
        merged_df = pd.merge(
            source_df,
            target_df,
            on=source_keys,
            how='outer',
            suffixes=('_src', '_tgt'),
            indicator=True
        )

        log_lines = []

        for _, row in merged_df.iterrows():
            join_key_vals = {k: row.get(k) for k in source_keys}
            if row['_merge'] == 'left_only':
                log_lines.append(f"Missing in Target: {row[source_keys].to_dict()}")
            elif row['_merge'] == 'right_only':
                log_lines.append(f"Missing in Source: {row[source_keys].to_dict()}")
            else:
                # Compare fields
                for field_map in compare_fields:
                    src_field = None
                    tgt_field = None

                    for k, v in field_map.items():
                        if k.startswith("SRC"):
                            src_field = v
                        elif k.startswith("TGT"):
                            tgt_field = v

                    src_val = row.get(src_field)
                    tgt_val = row.get(tgt_field)
                    if ("Date" in src_field or "DOB" in src_field or "Dt" in src_field or "StampTime" in src_field or "START_DT" in src_field or "END_DT" in src_field):
                        src_val = try_parse_date(src_val)
                        tgt_val = try_parse_date(tgt_val)

                    if pd.isna(src_val) and pd.isna(tgt_val):
                        continue
                    if src_val != tgt_val:
                        log_lines.append(
                            f"Mismatch at {join_key_vals}: Source[{src_field}] = {src_val} != Target[{tgt_field}] = {tgt_val}"
                        )
        # Write log
        with open(log_file, "w") as f:
            f.write("\n".join(log_lines))

        print(f"Comparison complete. {len(log_lines)} differences logged in '{log_file}'.")

    except Exception as e:
        print(e)
