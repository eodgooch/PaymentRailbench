# USDC is its own currency, never USD

Every amount is an integer in a currency's Minor Units, and USDC (6 decimals) is a separate currency from USD (2 decimals). Moving value between them is always an explicit conversion Transaction with its own rate and Entries. Treating USDC as dollars would bake the peg into the books: a depeg, a conversion fee, or rounding from 6 decimals to 2 would then show up as an unexplained imbalance instead of a visible conversion.
