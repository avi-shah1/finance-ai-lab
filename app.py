from flask import Flask, render_template, request, redirect, url_for

import database

app = Flask(__name__)


@app.route("/")
def home():
    companies = database.get_all_companies()
    return render_template("index.html", companies=companies)


@app.route("/add-company", methods=["POST"])
def add_company():
    database.insert_company(
        name=request.form["name"],
        website=request.form["website"],
        sector=request.form["sector"],
        country=request.form["country"],
        revenue=request.form["revenue"],
        employees=request.form["employees"],
        description=request.form["description"],
    )
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)
