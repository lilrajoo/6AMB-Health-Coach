def calc_bmi(height_cm, weight_kg):
    bmi = weight_kg / ((height_cm / 100) ** 2)
    if bmi < 18.5:  cat = "🔵 Underweight"
    elif bmi < 25:  cat = "🟢 Normal weight"
    elif bmi < 30:  cat = "🟡 Overweight"
    else:           cat = "🔴 Obese"
    return round(bmi, 1), cat


def calc_tdee(height_cm, weight_kg, age, gender):
    if gender.lower() == "male":
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    else:
        bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161
    return round(bmr * 1.2)


def get_calorie_note(total, tdee):
    ratio = total / tdee
    if ratio < 0.5:    return f"⚠️ Very low — under 50% of your daily target ({tdee} kcal)."
    elif ratio < 0.75: return f"🟡 Below target — aim for around {tdee} kcal today."
    elif ratio <= 1.0: return f"✅ On track — within your daily target of {tdee} kcal."
    elif ratio <= 1.2: return f"🟡 Slightly over your daily target of {tdee} kcal."
    else:              return f"🔴 Significantly over your daily target of {tdee} kcal."