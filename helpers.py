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
    ratio  = total / tdee
    excess = int(total - tdee)

    if ratio < 0.5:
        return f"\n⚠️ Very low — under 50% of your daily target ({tdee} kcal)."
    elif ratio < 0.75:
        return f"\n🟡 Below target — aim for around {tdee} kcal today."
    elif ratio <= 1.0:
        return f"\n✅ On track — within your daily target of {tdee} kcal."
    elif ratio <= 1.1:
        # Within 10% over — genuinely slightly over
        return f"\n🟡 Slightly over your daily target of {tdee} kcal by (+{excess} kcal)"
    elif ratio <= 1.3:
        # 10-30% over
        return f"\n🔴 Over your daily target by *{excess} kcal*. Consider a light workout to burn it off."
    else:
        # More than 30% over — significantly over
        return f"\n🔴 *Significantly over* your daily target by *{excess} kcal*. Try to burn it off with exercise!"