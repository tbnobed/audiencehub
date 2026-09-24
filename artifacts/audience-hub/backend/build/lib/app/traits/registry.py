"""User-facing catalog of computed profile traits.

The SQL expressions are the canonical expressions used by the set-based engine.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TraitDefinition:
    key: str
    label: str
    type: str
    description: str
    sql: str

    def public_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "description": self.description,
        }


TRAITS = (
    TraitDefinition("gift_count_total", "Lifetime gift count", "int", "All gifts recorded for the profile.", "gift_rollup.gift_count_total"),
    TraitDefinition("ltv_total", "Lifetime giving", "numeric", "Sum of all recorded gift amounts.", "gift_rollup.ltv_total"),
    TraitDefinition("gift_amount_12m", "Giving in the last 12 months", "numeric", "Sum of gift amounts dated within the last 365 days.", "gift_rollup.gift_amount_12m"),
    TraitDefinition("gift_count_12m", "Gifts in the last 12 months", "int", "Number of gifts dated within the last 365 days.", "gift_rollup.gift_count_12m"),
    TraitDefinition("first_gift_date", "First gift date", "date", "Date of the profile's first dated gift.", "gift_rollup.first_gift_date"),
    TraitDefinition("last_gift_date", "Last gift date", "date", "Date of the profile's most recent dated gift.", "gift_rollup.last_gift_date"),
    TraitDefinition("largest_gift_amount", "Largest gift", "numeric", "Largest amount among recorded gifts.", "gift_rollup.largest_gift_amount"),
    TraitDefinition("avg_gift_amount", "Average gift", "numeric", "Average amount among recorded gifts.", "gift_rollup.avg_gift_amount"),
    TraitDefinition("is_recurring_active", "Active recurring donor", "bool", "True when a recurring gift was made within the last 45 days.", "gift_rollup.is_recurring_active"),
    TraitDefinition("days_since_last_gift", "Days since last gift", "int", "Whole days between the pinned as_of date and last gift date.", "as_of - gift_rollup.last_gift_date"),
    TraitDefinition("donor_status", "Donor status", "text", "Prospect, new, active, reactivated, lapsing, or lapsed based on gift history.", "CASE: no last gift => prospect; age <=365 and first gift age <=365 => new; age <=365 after a gap >730 => reactivated; age <=365 => active; age <=730 => lapsing; else lapsed"),
    TraitDefinition("rfm_recency", "RFM recency", "smallint", "Donor-only recency quintile; 5 is most recent.", "6 - ntile(5) OVER (ORDER BY days_since_last_gift ASC, profile_id)"),
    TraitDefinition("rfm_frequency", "RFM frequency", "smallint", "Donor-only lifetime gift-count quintile; 5 is highest frequency.", "6 - ntile(5) OVER (ORDER BY gift_count_total DESC, profile_id)"),
    TraitDefinition("rfm_monetary", "RFM monetary", "smallint", "Donor-only lifetime giving quintile; 5 is highest giving.", "6 - ntile(5) OVER (ORDER BY ltv_total DESC, profile_id)"),
    TraitDefinition("rfm_score", "RFM score", "text", "Concatenated recency, frequency, and monetary quintile scores.", "rfm_recency || rfm_frequency || rfm_monetary"),
    TraitDefinition("event_count_30d", "Events in the last 30 days", "int", "Count of events recorded during the last 30 days.", "event_rollup.event_count_30d"),
    TraitDefinition("last_event_at", "Last event time", "timestamptz", "Timestamp of the most recent event.", "event_rollup.last_event_at"),
    TraitDefinition("video_views_30d", "Video views in the last 30 days", "int", "Count of events named Video Watched during the last 30 days.", "event_rollup.video_views_30d"),
    TraitDefinition("last_engagement_channel", "Last engagement channel", "text", "Source key for the most recent event or gift.", "engagement.source_key"),
    TraitDefinition("source_keys", "Source keys", "text[]", "Sorted unique source keys represented by records, gifts, or events.", "source_rollup.source_keys"),
    TraitDefinition("computed_at", "Traits computed at", "timestamptz", "Timestamp when this row of computed traits was generated.", "clock_timestamp()"),
)

TRAITS_BY_KEY = {trait.key: trait for trait in TRAITS}