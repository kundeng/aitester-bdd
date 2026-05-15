*** Settings ***
Documentation     Wikipedia smoke: main page renders + search for 'Behavior-driven development' opens the article
Library           aitester_bdd.AITester
Suite Setup       Given I start verification "${DEPLOYMENT}"
Suite Teardown    Then I finalize verification

*** Variables ***
${ENGINE}           agent-browser
${DEPLOYMENT}       wikipedia-smoke
${BASE_URL}         https://en.wikipedia.org
${SEARCH_QUERY}     Behavior-driven development

*** Test Cases ***
Main Page Renders And Search Opens Article
    [Setup]    Given I start scenario "wiki_smoke" at "${BASE_URL}"

    I define rule "main_page_renders"
        When I open "${BASE_URL}"
        And url contains "/wiki/Main_Page"
        Then selector "a.mw-logo[href='/wiki/Main_Page']" exists
        Then locator "a.mw-logo[href='/wiki/Main_Page'] img.mw-logo-wordmark" has attribute "alt" equal to "Wikipedia"
        Then selector "input#searchInput[name='search']" exists
        Then locator "input#searchInput" has attribute "placeholder" containing "Search Wikipedia"
        Then count of locator "#mp-itn a, #mp-otd a, #mp-dyk a" is at least 5

    I define rule "search_submits_to_article"
        And I declare parents "main_page_renders"
        When I type "${SEARCH_QUERY}" into locator "input#searchInput"
        When I press keys "input#searchInput"    Enter
        And url contains "/wiki/Behavior-driven_development"
        Then selector "h1#firstHeading" exists
        Then locator "h1#firstHeading" has text "Behavior-driven development"
        Then count of locator "#mw-content-text p:has-text(\"BDD\")" is at least 1
        Then selector "h2#References" exists
        Then locator "h2#References" has text "References"
